"""Tests for #148's "사용자별 기본 팩" registry column
(``packs.is_default``): ``opencrab.pack.ownership.ensure_default_pack`` /
``resolve_write_pack``, and the ``create_user`` / ``bootstrap_local_user``
wiring in ``opencrab.auth`` that creates a default pack in the same
transaction as the user row.

Fixture/style follows tests/test_packs_registry.py (real in-memory SQLite
SQLStore, no LOCAL_DATA_DIR needed) and tests/test_stores.py's
pre-migration-database pattern (hand-rolled legacy DDL on a real on-disk
file, then open it with SQLStore to exercise the migration path).
"""

from __future__ import annotations

import pytest

from opencrab.auth import Principal, bootstrap_local_user, create_user
from opencrab.pack.ownership import ensure_default_pack, get_pack, resolve_write_pack


@pytest.fixture
def sql():
    from opencrab.stores.sql_store import SQLStore

    return SQLStore("sqlite:///:memory:")


@pytest.fixture
def alice(sql):
    return create_user(sql, "Alice")


# ---------------------------------------------------------------------------
# create_user / bootstrap_local_user wiring
# ---------------------------------------------------------------------------


class TestCreateUserDefaultPack:
    def test_create_user_makes_a_default_pack_in_the_same_transaction(self, sql, alice):
        """Right after create_user returns, ensure_default_pack must find
        the pack that create_user already made -- NOT create a second one
        (that would mean the two ran in separate transactions, or worse,
        two default packs exist for one owner)."""
        rows_before = [
            row for row in _all_packs(sql) if row["owner_id"] == alice
        ]
        assert len(rows_before) == 1
        assert rows_before[0]["is_default"] is True

        found = ensure_default_pack(sql, alice)
        assert found == rows_before[0]["pack_id"]
        rows_after = [row for row in _all_packs(sql) if row["owner_id"] == alice]
        assert len(rows_after) == 1  # still exactly one -- no duplicate created

    def test_bootstrap_local_user_makes_a_default_pack_too(self, sql):
        user_id, _secret = bootstrap_local_user(sql)
        rows = [row for row in _all_packs(sql) if row["owner_id"] == user_id]
        assert len(rows) == 1
        assert rows[0]["is_default"] is True
        assert rows[0]["title"] == "Default pack"
        assert rows[0]["visibility"] == "private"


def _all_packs(sql):
    from sqlalchemy import text

    from opencrab.pack.ownership import _SELECT_COLS, _row_to_dict

    with sql._engine.connect() as conn:
        rows = conn.execute(text(f"SELECT {_SELECT_COLS} FROM packs")).fetchall()  # noqa: S608
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# ensure_default_pack: idempotence + legacy-user recovery
# ---------------------------------------------------------------------------


class TestEnsureDefaultPack:
    def test_idempotent_across_repeated_calls(self, sql, alice):
        first = ensure_default_pack(sql, alice)
        second = ensure_default_pack(sql, alice)
        third = ensure_default_pack(sql, alice)
        assert first == second == third

    def test_legacy_user_with_no_default_pack_gets_one_lazily(self, sql):
        """A user row created directly (bypassing create_user, simulating a
        pre-#148 row already in the database) has no default pack --
        resolve_write_pack must still hand back a usable pack_id."""
        from sqlalchemy import text

        with sql._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (user_id, display_name, is_local) "
                    "VALUES (:uid, :name, :is_local)"
                ),
                {"uid": "legacy-user", "name": "Legacy", "is_local": False},
            )
        assert _all_packs_for_owner(sql, "legacy-user") == []

        principal = Principal(user_id="legacy-user", is_local=False, disabled=False)
        pack_id = resolve_write_pack(sql, principal, None)
        rows = _all_packs_for_owner(sql, "legacy-user")
        assert len(rows) == 1
        assert rows[0]["pack_id"] == pack_id
        assert rows[0]["is_default"] is True

        # And it's idempotent from here on too.
        assert resolve_write_pack(sql, principal, None) == pack_id
        assert len(_all_packs_for_owner(sql, "legacy-user")) == 1

    def test_only_one_default_pack_per_owner_enforced_by_partial_unique_index(self, sql, alice):
        """A direct second INSERT with is_default=1 for the same owner must
        fail -- idx_packs_one_default is a real DB constraint, not just
        ensure_default_pack's own bookkeeping."""
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            with sql._engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO packs (pack_id, owner_id, visibility, title, is_default) "
                        "VALUES (:pid, :oid, 'private', 'Second default', 1)"
                    ),
                    {"pid": "second-default-pack", "oid": alice},
                )


def _all_packs_for_owner(sql, owner_id):
    return [row for row in _all_packs(sql) if row["owner_id"] == owner_id]


# ---------------------------------------------------------------------------
# resolve_write_pack
# ---------------------------------------------------------------------------


class TestResolveWritePack:
    def test_requested_pack_id_passes_through_unchanged(self, sql, alice):
        principal = Principal(user_id=alice, is_local=False, disabled=False)
        assert resolve_write_pack(sql, principal, "some-other-pack") == "some-other-pack"
        # No default pack lookup/creation happened as a side effect of
        # passing a requested id -- alice already has exactly one pack (her
        # default, made by create_user), and it's untouched.
        assert len(_all_packs_for_owner(sql, alice)) == 1

    def test_no_requested_pack_id_returns_the_default_pack(self, sql, alice):
        principal = Principal(user_id=alice, is_local=False, disabled=False)
        default_pack_id = ensure_default_pack(sql, alice)
        assert resolve_write_pack(sql, principal, None) == default_pack_id

    def test_empty_string_requested_falls_back_to_default_too(self, sql, alice):
        """Falsy, not just None, must fall back -- an empty-string pack_id
        should never make it through as a literal target."""
        principal = Principal(user_id=alice, is_local=False, disabled=False)
        default_pack_id = ensure_default_pack(sql, alice)
        assert resolve_write_pack(sql, principal, "") == default_pack_id


# ---------------------------------------------------------------------------
# _row_to_dict normalizes is_default to bool
# ---------------------------------------------------------------------------


class TestRowToDictIsDefault:
    def test_is_default_is_a_real_bool_not_0_or_1(self, sql, alice):
        pack_id = ensure_default_pack(sql, alice)
        row = get_pack(sql, pack_id)
        assert row["is_default"] is True
        assert isinstance(row["is_default"], bool)

    def test_non_default_pack_reports_false(self, sql, alice):
        from opencrab.pack.ownership import create_pack

        pack_id = create_pack(sql, alice, "not-default")
        row = get_pack(sql, pack_id)
        assert row["is_default"] is False
        assert isinstance(row["is_default"], bool)


# ---------------------------------------------------------------------------
# Migration: an existing pre-#148 database gains is_default on open, and
# reopening it again (simulated restart) is a no-op, not an error.
# ---------------------------------------------------------------------------


_LEGACY_USERS_DDL = """
CREATE TABLE users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    is_local     INTEGER NOT NULL DEFAULT 0 CHECK (is_local IN (0, 1)),
    disabled     INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at   TEXT DEFAULT (datetime('now'))
)
"""

# Pre-#148 shape: no is_default column at all.
_LEGACY_PACKS_DDL = """
CREATE TABLE packs (
    pack_id      TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL REFERENCES users (user_id),
    visibility   TEXT NOT NULL DEFAULT 'private',
    title        TEXT,
    description  TEXT,
    forked_from  TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
)
"""


class TestMigration:
    def test_opening_a_legacy_db_adds_is_default_column(self, tmp_path):
        from sqlalchemy import create_engine, text

        from opencrab.stores.sql_store import SQLStore

        db_path = tmp_path / "legacy.db"
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(text(_LEGACY_USERS_DDL))
            conn.execute(text(_LEGACY_PACKS_DDL))
            conn.execute(
                text(
                    "INSERT INTO users (user_id, display_name, is_local) "
                    "VALUES ('u1', 'Old User', 0)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO packs (pack_id, owner_id, visibility, title) "
                    "VALUES ('old-pack', 'u1', 'private', 'Pre-existing pack')"
                )
            )
        engine.dispose()

        store = SQLStore(f"sqlite:///{db_path}")
        with store._engine.connect() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(packs)"))}
        assert "is_default" in cols

        # The pre-existing row backfilled to the column default (0/False),
        # not left NULL or made the default by surprise.
        row = get_pack(store, "old-pack")
        assert row["is_default"] is False

        # The legacy user can still be lazily recovered into having a
        # default pack via resolve_write_pack (test_legacy_user_with_no_...
        # above covers the mechanics in-memory; this pins it on a real
        # migrated on-disk DB too).
        principal = Principal(user_id="u1", is_local=False, disabled=False)
        pack_id = resolve_write_pack(store, principal, None)
        assert get_pack(store, pack_id)["is_default"] is True

    def test_reopening_a_migrated_db_is_idempotent(self, tmp_path):
        """Simulates a process restart against an already-migrated DB --
        must not raise (e.g. from a naive unconditional ALTER TABLE ADD
        COLUMN, which SQLite rejects the second time as duplicate column)."""
        from sqlalchemy import create_engine, text

        from opencrab.stores.sql_store import SQLStore

        db_path = tmp_path / "legacy.db"
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(text(_LEGACY_USERS_DDL))
            conn.execute(text(_LEGACY_PACKS_DDL))
        engine.dispose()

        SQLStore(f"sqlite:///{db_path}")  # first open: runs the migration
        store2 = SQLStore(f"sqlite:///{db_path}")  # second open: must be a no-op, not an error
        assert store2.available is True

        alice = create_user(store2, "Alice")
        assert ensure_default_pack(store2, alice)
