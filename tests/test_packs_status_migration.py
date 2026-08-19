"""Tests for #170's ``packs.status`` column and its additive migration
(``opencrab.stores.sql_store.SQLStore._migrate_packs_status``).

Scope: schema + migration mechanics only (design v4 §3.1, §3.8). The
lifecycle semantics that *use* this column -- ``ownership.py``'s
``begin_pack_creation``/``mark_pack_ready``/``mark_pack_partial``, the
write gate, ``pack_create``'s rewrite, and the repair CLI -- are a
different worker's files and are covered by their own test modules. This
file pins two things and nothing else:

1. The column itself: a fresh DB gets it with the right default and CHECK
   domain, and a pre-#170 DB gets it added additively, idempotently, with
   every existing row landing ``'ready'`` (never left NULL, never silently
   dropped into some other value).
2. That the migration scripts in ``scripts/_migration_tables.py`` stay
   consistent with the asymmetric copy rule they already implement
   (design v4 §3.8): a column only the *target* has is filled by the
   target's default, a column only the *source* has is a hard error. This
   is not new behaviour in ``_migration_tables.py`` -- ``status`` simply
   must never be added to ``required_columns`` there, because the whole
   point of the ``'ready'`` default (see
   ``SQLStore._migrate_packs_status``'s docstring) is that a pre-#170
   source can be migrated forward without ever having heard of this
   column.

Style follows ``tests/test_default_pack.py``'s ``TestMigration`` (hand-rolled
legacy DDL on a real on-disk SQLite file, then opened through ``SQLStore`` to
exercise the migration path) and ``tests/test_migration_guards.py`` (pure
function tests against ``scripts/_migration_tables.py`` with no live
PostgreSQL). No live PostgreSQL is available in this environment, so the
PG-dialect ALTER branch (test 6 below) is pinned at the source level rather
than executed -- see ``TestPgDialectBranch`` for why, and why
``pg_typed_columns`` (test 7's third bullet, folded into
``TestMigrationScriptAlignment``) can still be exercised for real against a
SQLite engine despite being named for its PostgreSQL authority.
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import _migration_tables as mt  # noqa: E402

from opencrab.stores.sql_store import SQLStore  # noqa: E402

PACKS_STATUSES = ("creating", "partial", "ready")


@pytest.fixture
def sql() -> SQLStore:
    return SQLStore("sqlite:///:memory:")


def _insert_user(sql: SQLStore, user_id: str) -> None:
    with sql._engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (user_id, display_name, is_local) VALUES (:uid, :name, 0)"),
            {"uid": user_id, "name": user_id},
        )


# ---------------------------------------------------------------------------
# 1. Fresh DB: column exists, default is 'ready'
# ---------------------------------------------------------------------------


class TestFreshDatabase:
    def test_status_column_exists_with_ready_default(self, sql: SQLStore) -> None:
        """A brand-new in-memory DB gets ``status`` straight from the static
        DDL (not the migration path -- that's TestLegacyMigration below).
        An INSERT that omits ``status`` entirely must land ``'ready'``: the
        column default, not application code, is what makes an ordinary
        ``create_pack``-style insert (which predates #170 and may not name
        every new column on day one) come out visible rather than NULL or
        rejected by the NOT NULL constraint."""
        _insert_user(sql, "u1")
        with sql._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO packs (pack_id, owner_id, visibility, title) "
                    "VALUES ('p1', 'u1', 'private', 'T')"
                )
            )
        with sql._engine.connect() as conn:
            row = conn.execute(text("SELECT status FROM packs WHERE pack_id = 'p1'")).fetchone()
        assert row is not None
        assert row[0] == "ready"

    def test_column_present_in_pragma(self, sql: SQLStore) -> None:
        with sql._engine.connect() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(packs)"))}
        assert "status" in cols


# ---------------------------------------------------------------------------
# 2. CHECK constraint rejects values outside the enum
# ---------------------------------------------------------------------------


class TestCheckConstraint:
    def test_bogus_status_is_rejected(self, sql: SQLStore) -> None:
        _insert_user(sql, "u1")
        with pytest.raises(IntegrityError):
            with sql._engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO packs (pack_id, owner_id, visibility, title, status) "
                        "VALUES ('p1', 'u1', 'private', 'T', 'bogus')"
                    )
                )

    @pytest.mark.parametrize("status", PACKS_STATUSES)
    def test_each_declared_status_is_accepted(self, sql: SQLStore, status: str) -> None:
        """The inverse of the bogus-value test -- pins the exact domain
        design v4 §3.2 names (creating/partial/ready), not just "something
        gets rejected"."""
        _insert_user(sql, "u1")
        with sql._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO packs (pack_id, owner_id, visibility, title, status) "
                    "VALUES (:pid, 'u1', 'private', 'T', :status)"
                ),
                {"pid": f"p-{status}", "status": status},
            )
        with sql._engine.connect() as conn:
            row = conn.execute(
                text("SELECT status FROM packs WHERE pack_id = :pid"), {"pid": f"p-{status}"}
            ).fetchone()
        assert row[0] == status


# ---------------------------------------------------------------------------
# 3/4/5. Legacy DB migration: column added, existing rows all 'ready',
# idempotent across repeated opens, index created.
# ---------------------------------------------------------------------------

# Pre-#170 shape: users + packs (with is_default, #148's column) but no
# status column at all -- the exact schema SQLStore wrote right before this
# PR.
_LEGACY_USERS_DDL = """
CREATE TABLE users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    is_local     INTEGER NOT NULL DEFAULT 0 CHECK (is_local IN (0, 1)),
    disabled     INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at   TEXT DEFAULT (datetime('now'))
);
"""

_LEGACY_PACKS_DDL = """
CREATE TABLE packs (
    pack_id      TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL REFERENCES users (user_id),
    visibility   TEXT NOT NULL DEFAULT 'private',
    title        TEXT,
    description  TEXT,
    forked_from  TEXT,
    is_default   INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
)
"""


def _make_legacy_db(path: Path) -> str:
    """A pre-#170 on-disk SQLite file: users + packs, no status column, a
    handful of pre-existing rows (mirrors real installs, which always have
    at least one pack -- the owner's default pack)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_LEGACY_USERS_DDL + _LEGACY_PACKS_DDL)
        conn.executescript(
            """
            INSERT INTO users (user_id, display_name, is_local) VALUES ('alice', 'Alice', 0);
            INSERT INTO users (user_id, display_name, is_local) VALUES ('bob', 'Bob', 0);
            INSERT INTO packs (pack_id, owner_id, visibility, title, is_default)
                VALUES ('alice-default', 'alice', 'private', 'Default pack', 1);
            INSERT INTO packs (pack_id, owner_id, visibility, title, is_default)
                VALUES ('bob-default', 'bob', 'private', 'Default pack', 1);
            INSERT INTO packs (pack_id, owner_id, visibility, title, is_default)
                VALUES ('shared-pack', 'alice', 'public', 'Shared', 0);
            """
        )
        conn.commit()
    finally:
        conn.close()
    return str(path)


class TestLegacyMigration:
    def test_opening_a_legacy_db_adds_status_column_and_backfills_ready(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "legacy.db"
        _make_legacy_db(db_path)

        store = SQLStore(f"sqlite:///{db_path}")
        with store._engine.connect() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(packs)"))}
            rows = conn.execute(text("SELECT pack_id, status FROM packs")).fetchall()
        assert "status" in cols
        assert len(rows) == 3  # the migration touches the column, not row count
        assert {pack_id: status for pack_id, status in rows} == {
            "alice-default": "ready",
            "bob-default": "ready",
            "shared-pack": "ready",
        }

    def test_reopening_a_migrated_db_is_idempotent(self, tmp_path: Path) -> None:
        """Two more opens (simulating process restarts) must not raise (a
        naive unconditional ALTER TABLE ADD COLUMN would fail the second
        time with 'duplicate column') and must not change the values that
        are already there."""
        db_path = tmp_path / "legacy.db"
        _make_legacy_db(db_path)

        SQLStore(f"sqlite:///{db_path}")  # first open: runs the migration
        store2 = SQLStore(f"sqlite:///{db_path}")  # second open: no-op
        store3 = SQLStore(f"sqlite:///{db_path}")  # third open: still a no-op
        assert store2.available is True
        assert store3.available is True

        with store3._engine.connect() as conn:
            rows = conn.execute(text("SELECT pack_id, status FROM packs")).fetchall()
        assert {pack_id: status for pack_id, status in rows} == {
            "alice-default": "ready",
            "bob-default": "ready",
            "shared-pack": "ready",
        }

    def test_migrated_db_still_enforces_the_check_constraint(self, tmp_path: Path) -> None:
        """The ALTER-added column must carry the same CHECK domain as a
        from-scratch column, not just NOT NULL DEFAULT -- SQLite's ADD
        COLUMN syntax allows attaching a CHECK, and #148's ``is_default``
        migration already relies on that (see its ALTER statement), so this
        pins that the status ALTER does the same rather than silently
        dropping the constraint."""
        db_path = tmp_path / "legacy.db"
        _make_legacy_db(db_path)
        store = SQLStore(f"sqlite:///{db_path}")

        with pytest.raises(IntegrityError):
            with store._engine.begin() as conn:
                conn.execute(
                    text("UPDATE packs SET status = 'bogus' WHERE pack_id = 'alice-default'")
                )

    def test_idx_packs_status_created_on_legacy_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy.db"
        _make_legacy_db(db_path)
        store = SQLStore(f"sqlite:///{db_path}")

        with store._engine.connect() as conn:
            names = {
                row[0]
                for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'index'")
                )
            }
        assert "idx_packs_status" in names

    def test_idx_packs_status_created_on_fresh_db(self, sql: SQLStore) -> None:
        """The static DDL lists don't declare this index (design v4 §3.1:
        it lives in the migration method precisely so it never races ahead
        of the column on an old DB) -- so a from-scratch DB must get it from
        ``_migrate_packs_status`` too, not just the legacy path above."""
        with sql._engine.connect() as conn:
            names = {
                row[0]
                for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'index'")
                )
            }
        assert "idx_packs_status" in names


# ---------------------------------------------------------------------------
# 6. PostgreSQL dialect branch, pinned without a live PostgreSQL.
#
# Executing the PG branch for real would need a live server -- there isn't
# one in this environment (see test_migration_guards.py's module docstring,
# same constraint). Building a fake Connection to intercept
# ``conn.execute(text(...))`` calls was considered and rejected: the branch
# is guarded by ``conn.begin_nested()`` (a SAVEPOINT), and a fake connection
# would have to also fake that context manager plus the two-query shape
# (existence check, then conditionally the ALTER, then unconditionally the
# CREATE INDEX) to be worth trusting -- at that point the fake is a second
# implementation of the method that could drift from the real one and hide
# exactly the kind of bug this test exists to catch. Design v4 explicitly
# allows a source-level assertion as the fallback for this case, so this
# reads the method's actual source text and checks the PG-only literals
# design v4 §3.1 requires: the ``information_schema`` query scoped to
# ``current_schema()`` (not just ``table_name = 'packs'`` -- that's the
# cross-schema bug #148's is_default migration already had to guard
# against, see its own comment) and the ``VARCHAR(32)`` + ``CHECK`` column
# definition on the ALTER.
# ---------------------------------------------------------------------------


class TestPgDialectBranch:
    def test_information_schema_query_is_scoped_to_current_schema(self) -> None:
        source = inspect.getsource(SQLStore._migrate_packs_status)
        assert "information_schema.columns" in source
        assert "table_schema = current_schema()" in source
        assert "table_name = 'packs'" in source

    def test_pg_alter_uses_varchar32_with_check(self) -> None:
        source = inspect.getsource(SQLStore._migrate_packs_status)
        assert "ALTER TABLE packs ADD COLUMN status VARCHAR(32)" in source
        assert "CHECK (status IN ('creating', 'partial', 'ready'))" in source

    def test_pg_alter_is_not_null_with_ready_default(self) -> None:
        source = inspect.getsource(SQLStore._migrate_packs_status)
        # Looser than a single literal match: the ALTER text is built across
        # two adjacent string literals in the source, so this checks the
        # ingredients are present near each other rather than pinning exact
        # line wrapping.
        pg_branch = source.split("else:", 1)[1]
        assert "NOT NULL DEFAULT 'ready'" in pg_branch

    def test_duplicate_column_and_already_exists_are_absorbed(self) -> None:
        """The TOCTOU-absorbing except clause (copied from
        ``_migrate_packs_is_default``) must still name both driver-specific
        phrasings -- SQLite says "duplicate column", PostgreSQL says
        "already exists" -- or a racing second process crashes instead of
        treating the loser's ALTER failure as a no-op."""
        source = inspect.getsource(SQLStore._migrate_packs_status)
        assert '"duplicate column"' in source
        assert '"already exists"' in source


# ---------------------------------------------------------------------------
# 7. Migration script alignment (design v4 §3.8).
# ---------------------------------------------------------------------------


class TestMigrationScriptAlignment:
    def test_packs_spec_does_not_require_status(self) -> None:
        """If a future edit added ``status`` to ``required_columns``, every
        pre-#170 source (which by definition lacks the column) would hard-
        error out of ``resolve_columns`` -- exactly the "silent partial
        copy" failure mode #151 exists to prevent, except inverted: here it
        would block an otherwise-clean migration instead of silently
        dropping data. The whole point of the ``'ready'`` column default
        (see ``SQLStore._migrate_packs_status``'s docstring) is that
        ``status`` is allowed to be target-only."""
        spec = mt.SPEC_BY_NAME["packs"]
        assert "status" not in spec.required_columns

    def test_resolve_columns_drops_status_when_only_target_has_it(self) -> None:
        """Forward migration shape: an old SQLite source has no status
        column, a #170-or-later PostgreSQL target does. resolve_columns
        must not error and must not include 'status' in the copy list --
        the target's own column default fills it in (design v4 §3.8)."""
        spec = mt.SPEC_BY_NAME["packs"]
        src_columns = [
            "pack_id",
            "owner_id",
            "visibility",
            "title",
            "description",
            "forked_from",
            "created_at",
            "updated_at",
        ]
        dst_columns = [*src_columns, "is_default", "status"]

        resolved = mt.resolve_columns(spec, src_columns, dst_columns)

        assert "status" not in resolved
        assert set(resolved) == set(src_columns)

    def test_resolve_columns_errors_when_only_source_has_it(self) -> None:
        """The asymmetric half of the rule: if the *source* somehow has a
        status column the target lacks (e.g. a reverse migration onto an
        older SQLite build), resolve_columns must refuse rather than
        silently drop it -- a MigrationError, not a warning."""
        spec = mt.SPEC_BY_NAME["packs"]
        src_columns = [
            "pack_id",
            "owner_id",
            "visibility",
            "title",
            "description",
            "forked_from",
            "created_at",
            "updated_at",
            "status",
        ]
        dst_columns = [c for c in src_columns if c != "status"]

        with pytest.raises(mt.MigrationError, match="status"):
            mt.resolve_columns(spec, src_columns, dst_columns)

    def test_pg_typed_columns_does_not_classify_status_as_boolean_or_timestamp(
        self, sql: SQLStore
    ) -> None:
        """``pg_typed_columns`` is generic SQLAlchemy reflection (``inspect
        (engine).get_columns(table)``) -- it is named for its PostgreSQL
        authority (real BOOLEAN/TIMESTAMPTZ types vs. SQLite's untyped
        INTEGER/TEXT), not restricted to a PostgreSQL connection, so it can
        be exercised for real here without one. What this proves either way:
        'status' is declared VARCHAR/TEXT, a String-family type, so it can
        never be reflected as Boolean or DateTime -- confirming it stays out
        of both conversion sets regardless of which engine reflects it."""
        names, booleans, timestamps = mt.pg_typed_columns(sql._engine, "packs")
        assert "status" in names
        assert "status" not in booleans
        assert "status" not in timestamps

    def test_forward_migration_lands_ready_on_the_target(self, tmp_path: Path) -> None:
        """End-to-end simulation of design v4 §3.8's acceptance line:
        forward migration (old SQLite source -> status-bearing target),
        using resolve_columns' own output as the copy list, must land
        'ready' on the target row -- not because the test assumes it, but
        because the target's column default does. Both sides are SQLite so
        no live PostgreSQL is needed (the target's DDL is identical in
        shape to PostgreSQL's for this purpose: NOT NULL DEFAULT 'ready')."""
        spec = mt.SPEC_BY_NAME["packs"]

        # Source: a legacy on-disk SQLite file, no status column.
        src_path = tmp_path / "source.db"
        _make_legacy_db(src_path)
        src_conn = sqlite3.connect(str(src_path))
        try:
            src_columns = mt.sqlite_columns(src_conn, "packs")
            src_row = src_conn.execute(
                "SELECT * FROM packs WHERE pack_id = 'shared-pack'"
            ).fetchone()
        finally:
            src_conn.close()

        # Target: a real post-#170 SQLStore, which has the status column.
        dst = SQLStore("sqlite:///:memory:")
        _insert_user(dst, "alice")
        with dst._engine.connect() as conn:
            dst_columns = [row[1] for row in conn.execute(text("PRAGMA table_info(packs)"))]

        copy_columns = mt.resolve_columns(spec, src_columns, dst_columns)
        assert "status" not in copy_columns

        values = dict(zip(src_columns, src_row, strict=True))
        placeholders = ", ".join(f":{c}" for c in copy_columns)
        col_list = ", ".join(copy_columns)
        with dst._engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO packs ({col_list}) VALUES ({placeholders})"),  # noqa: S608
                {c: values[c] for c in copy_columns},
            )

        with dst._engine.connect() as conn:
            row = conn.execute(
                text("SELECT status FROM packs WHERE pack_id = 'shared-pack'")
            ).fetchone()
        assert row is not None
        assert row[0] == "ready"
