"""opencrab.pack.ownership (#146, execution 3 of #143) + the MCP tools that
build on it: pack_create's slug-collision handling and the new pack_publish
tool.

Not to be confused with tests/test_pack_registry.py, which covers the
UNRELATED on-disk manifest registry (opencrab.ontology.pack_registry) --
this file is about the new `packs` SQL table (owner_id/visibility).

Registry-level tests use a real in-memory SQLite SQLStore (same style as
tests/test_auth.py) -- no LOCAL_DATA_DIR/scratch dir needed. MCP-tool-level
tests patch _get_context the same way tests/test_tools_handlers_direct.py
does, wiring a real SQLStore into ctx["sql"] so pack_create/pack_publish
exercise the real registry.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from opencrab.auth import Principal, create_user, principal_scope
from opencrab.pack.ownership import (
    PackForbiddenError,
    PackNotFoundError,
    _insert_pack,
    _is_pack_id_conflict,
    assert_writable,
    create_pack,
    get_pack,
    list_packs_for,
    readable_pack_ids,
    set_visibility,
)


@pytest.fixture
def sql():
    from opencrab.stores.sql_store import SQLStore

    return SQLStore("sqlite:///:memory:")


@pytest.fixture
def alice(sql):
    return create_user(sql, "Alice")


@pytest.fixture
def bob(sql):
    return create_user(sql, "Bob")


# ---------------------------------------------------------------------------
# create_pack
# ---------------------------------------------------------------------------


class TestCreatePack:
    def test_new_pack_is_private_and_owned_by_caller(self, sql, alice):
        pack_id = create_pack(sql, alice, "my-pack", title="My Pack")
        assert pack_id == "my-pack"
        row = get_pack(sql, "my-pack")
        assert row["owner_id"] == alice
        assert row["visibility"] == "private"
        assert row["title"] == "My Pack"

    def test_colliding_slug_is_quietly_suffixed_not_an_error(self, sql, alice, bob):
        """#146 B: on collision the retry draws a random suffix (not a
        sequential -2, -3, ... counter -- see create_pack's docstring for
        why a sequential counter would leak how many others hold the exact
        slug), so we assert the *shape* of the assigned id rather than an
        exact value: starts with the requested slug, differs from it,
        isn't the old sequential pattern, and resolves immediately."""
        first = create_pack(sql, alice, "coffee", title="Alice's coffee notes")
        second = create_pack(sql, bob, "coffee", title="Bob's coffee notes")
        assert first == "coffee"
        assert second != "coffee"
        assert second.startswith("coffee-")
        assert second not in ("coffee-2", "coffee-3")
        # Both rows exist, correctly owned -- no exception, no shared identity.
        assert get_pack(sql, "coffee")["owner_id"] == alice
        assert get_pack(sql, second)["owner_id"] == bob

    def test_three_way_collision_each_gets_a_distinct_random_suffix(self, sql, alice, bob):
        carol = create_user(sql, "Carol")
        first = create_pack(sql, alice, "coffee")
        second = create_pack(sql, bob, "coffee")
        third = create_pack(sql, carol, "coffee")
        assert first == "coffee"
        assert len({first, second, third}) == 3  # all distinct
        for pack_id in (second, third):
            assert pack_id.startswith("coffee-")
            assert pack_id not in ("coffee-2", "coffee-3")
            assert get_pack(sql, pack_id) is not None

    def test_b1_single_collision_gets_one_random_retry(self, sql, alice, bob, monkeypatch):
        """B1: forces the exact retry path -- first candidate taken, the
        very next random draw succeeds -- and pins that _insert_pack is
        called exactly twice (no sequential attempts in between)."""
        import opencrab.pack.ownership as registry_mod

        create_pack(sql, alice, "coffee")
        monkeypatch.setattr(registry_mod.secrets, "token_hex", lambda n: "aaaa0000")
        calls: list[str] = []
        real_insert = registry_mod._insert_pack

        def _counting_insert(sql_, pack_id, *rest):
            calls.append(pack_id)
            return real_insert(sql_, pack_id, *rest)

        monkeypatch.setattr(registry_mod, "_insert_pack", _counting_insert)

        assigned = registry_mod.create_pack(sql, bob, "coffee")
        assert assigned == "coffee-aaaa0000"
        assert calls == ["coffee", "coffee-aaaa0000"]
        assert get_pack(sql, "coffee-aaaa0000")["owner_id"] == bob

    def test_b2_exhausting_all_random_attempts_raises_and_leaves_no_row(
        self, sql, alice, bob, monkeypatch
    ):
        """B2: every one of the 8 random retries collides (token_hex is
        monkeypatched to always draw the same already-occupied candidate) ->
        RuntimeError, and the registry ends up with no new row for the
        failed call (only the pre-seeded "coffee" and "coffee-deadbeef"
        rows exist)."""
        import opencrab.pack.ownership as registry_mod

        create_pack(sql, alice, "coffee")
        create_pack(sql, alice, "coffee-deadbeef")  # pre-occupy every future random candidate
        monkeypatch.setattr(registry_mod.secrets, "token_hex", lambda n: "deadbeef")

        before = {row["pack_id"] for row in list_packs_for(sql, Principal(user_id=alice, is_local=False, disabled=False))}
        with pytest.raises(RuntimeError):
            registry_mod.create_pack(sql, bob, "coffee")
        after = {row["pack_id"] for row in list_packs_for(sql, Principal(user_id=alice, is_local=False, disabled=False))}
        assert after == before

    def test_forked_from_is_recorded(self, sql, alice):
        create_pack(sql, alice, "origin")
        pack_id = create_pack(sql, alice, "fork-of-origin", forked_from="origin")
        assert get_pack(sql, pack_id)["forked_from"] == "origin"


# ---------------------------------------------------------------------------
# _is_pack_id_conflict / _insert_pack — IntegrityError classification (#146
# pre-fix E: blindly treating every IntegrityError as "slug taken" would
# retry/misreport FK, CHECK, and NOT NULL violations as collisions too).
# ---------------------------------------------------------------------------


class _FakeOrig:
    """Minimal double for the driver-native exception ``exc.orig`` wraps --
    a bare MagicMock would answer ``getattr(mock, "pgcode", None)`` with
    another MagicMock (never None), defeating the classification's
    attribute-presence checks."""

    def __init__(self, message: str = "", **attrs):
        self._message = message
        for k, v in attrs.items():
            setattr(self, k, v)

    def __str__(self) -> str:
        return self._message


class _FakeExc:
    def __init__(self, orig):
        self.orig = orig


class TestIsPackIdConflict:
    """E2a: classification unit tests (codex-mandated -- a mutant that makes
    ``_insert_pack`` swallow every IntegrityError still passes an E1-only
    suite; these pin the classifier itself)."""

    def test_sqlite_primary_key_violation_on_pack_id_is_conflict(self):
        exc = _FakeExc(_FakeOrig(
            "UNIQUE constraint failed: packs.pack_id",
            sqlite_errorname="SQLITE_CONSTRAINT_PRIMARYKEY",
        ))
        assert _is_pack_id_conflict(exc) is True

    def test_sqlite_unique_violation_on_pack_id_is_conflict(self):
        exc = _FakeExc(_FakeOrig(
            "UNIQUE constraint failed: packs.pack_id",
            sqlite_errorname="SQLITE_CONSTRAINT_UNIQUE",
        ))
        assert _is_pack_id_conflict(exc) is True

    def test_sqlite_foreign_key_violation_is_not_conflict(self):
        exc = _FakeExc(_FakeOrig(
            "FOREIGN KEY constraint failed",
            sqlite_errorname="SQLITE_CONSTRAINT_FOREIGNKEY",
        ))
        assert _is_pack_id_conflict(exc) is False

    def test_sqlite_unique_violation_on_other_column_is_not_conflict(self):
        """A UNIQUE violation naming a different table/column must not be
        misread as a pack_id slug collision."""
        exc = _FakeExc(_FakeOrig(
            "UNIQUE constraint failed: users.email",
            sqlite_errorname="SQLITE_CONSTRAINT_UNIQUE",
        ))
        assert _is_pack_id_conflict(exc) is False

    def test_postgres_unique_violation_is_conflict(self):
        exc = _FakeExc(_FakeOrig(
            "duplicate key value violates unique constraint",
            pgcode="23505",
        ))
        assert _is_pack_id_conflict(exc) is True

    def test_postgres_foreign_key_violation_is_not_conflict(self):
        exc = _FakeExc(_FakeOrig(
            "insert or update on table violates foreign key constraint",
            pgcode="23503",
        ))
        assert _is_pack_id_conflict(exc) is False

    def test_unrecognised_orig_form_is_not_conflict(self):
        """No sqlite_errorname, no pgcode -- fail closed (re-raise), never
        guess "must be a collision"."""
        exc = _FakeExc(_FakeOrig("some other db error"))
        assert _is_pack_id_conflict(exc) is False


class TestInsertPackClassification:
    def test_e1_same_pack_id_reinsert_returns_false(self, sql, alice):
        """E1: the original collision-detection contract, unchanged."""
        assert _insert_pack(sql, "dup", alice, None, None, None) is True
        assert _insert_pack(sql, "dup", alice, None, None, None) is False

    def test_e2b_fk_violation_reraises_without_any_retry(self, sql, monkeypatch):
        """E2b: a real FK-shaped IntegrityError (owner_id referencing a
        nonexistent user, with the SQLite connection's FK pragma turned on
        for this test's engine only) must propagate out of create_pack
        rather than being swallowed as "slug taken" -- and create_pack must
        not have retried at all (a mutant that classifies FK errors as
        conflicts would otherwise silently exhaust every retry attempt and
        raise a misleading RuntimeError instead of the real IntegrityError)."""
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        import opencrab.pack.ownership as registry_mod

        # sql's engine uses SQLAlchemy's SingletonThreadPool for
        # sqlite:///:memory: (one physical connection per thread, reused
        # across every .connect()/.begin() call) -- setting the pragma once
        # here persists for every later connection this test's thread makes,
        # unlike a real multi-connection SQLite pool where it would need to
        # be set per-connection (e.g. via a "connect" event).
        with sql._engine.connect() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.commit()

        calls: list[str] = []
        real_insert = registry_mod._insert_pack

        def _counting_insert(*args, **kwargs):
            calls.append(args[1])  # pack_id positional arg
            return real_insert(*args, **kwargs)

        monkeypatch.setattr(registry_mod, "_insert_pack", _counting_insert)

        with pytest.raises(IntegrityError):
            registry_mod.create_pack(sql, "no-such-user", "orphan-pack")
        assert len(calls) == 1
        assert get_pack(sql, "orphan-pack") is None


# ---------------------------------------------------------------------------
# readable_pack_ids / list_packs_for
# ---------------------------------------------------------------------------


class TestReadablePackIds:
    def test_owner_sees_own_private_pack(self, sql, alice):
        create_pack(sql, alice, "mine")
        principal = Principal(user_id=alice, is_local=False, disabled=False)
        assert readable_pack_ids(sql, principal) == {"mine"}

    def test_non_owner_does_not_see_private_pack(self, sql, alice, bob):
        create_pack(sql, alice, "mine")
        principal = Principal(user_id=bob, is_local=False, disabled=False)
        assert readable_pack_ids(sql, principal) == set()

    def test_non_owner_sees_public_read_pack(self, sql, alice, bob):
        create_pack(sql, alice, "shared")
        set_visibility(
            sql, Principal(user_id=alice, is_local=False, disabled=False), "shared", "public-read"
        )
        principal = Principal(user_id=bob, is_local=False, disabled=False)
        assert readable_pack_ids(sql, principal) == {"shared"}

    def test_non_owner_sees_public_fork_pack(self, sql, alice, bob):
        create_pack(sql, alice, "forkable")
        set_visibility(
            sql, Principal(user_id=alice, is_local=False, disabled=False), "forkable", "public-fork"
        )
        principal = Principal(user_id=bob, is_local=False, disabled=False)
        assert readable_pack_ids(sql, principal) == {"forkable"}

    def test_mixed_visibility_scoping(self, sql, alice, bob):
        create_pack(sql, alice, "alice-private")
        create_pack(sql, alice, "alice-public")
        set_visibility(
            sql,
            Principal(user_id=alice, is_local=False, disabled=False),
            "alice-public",
            "public-read",
        )
        create_pack(sql, bob, "bob-private")

        bob_principal = Principal(user_id=bob, is_local=False, disabled=False)
        assert readable_pack_ids(sql, bob_principal) == {"alice-public", "bob-private"}

    def test_list_packs_for_matches_readable_pack_ids(self, sql, alice, bob):
        create_pack(sql, alice, "alice-private")
        create_pack(sql, alice, "alice-public")
        set_visibility(
            sql,
            Principal(user_id=alice, is_local=False, disabled=False),
            "alice-public",
            "public-read",
        )
        bob_principal = Principal(user_id=bob, is_local=False, disabled=False)
        rows = list_packs_for(sql, bob_principal)
        assert {r["pack_id"] for r in rows} == readable_pack_ids(sql, bob_principal)
        assert {r["pack_id"] for r in rows} == {"alice-public"}


# ---------------------------------------------------------------------------
# assert_writable / set_visibility — invariant 7 (existence must not leak)
# ---------------------------------------------------------------------------


class TestAssertWritable:
    def test_owner_can_write_own_private_pack(self, sql, alice):
        create_pack(sql, alice, "mine")
        principal = Principal(user_id=alice, is_local=False, disabled=False)
        pack = assert_writable(sql, principal, "mine")
        assert pack["pack_id"] == "mine"

    def test_nonexistent_pack_raises_not_found(self, sql, alice):
        principal = Principal(user_id=alice, is_local=False, disabled=False)
        with pytest.raises(PackNotFoundError):
            assert_writable(sql, principal, "does-not-exist")

    def test_someone_elses_private_pack_raises_not_found_not_forbidden(self, sql, alice, bob):
        """#143 invariant 7: a private pack owned by someone else must look
        IDENTICAL (same exception type) to a pack that doesn't exist at
        all -- PackForbiddenError would leak "this exists, you're just not
        allowed", which is exactly the signal invariant 7 forbids."""
        create_pack(sql, alice, "alice-secret")
        bob_principal = Principal(user_id=bob, is_local=False, disabled=False)
        with pytest.raises(PackNotFoundError):
            assert_writable(sql, bob_principal, "alice-secret")

    def test_someone_elses_public_pack_raises_forbidden(self, sql, alice, bob):
        """A VISIBLE pack (already observable via content_pack_list) can
        distinguish "forbidden" from "not found" -- no new leak."""
        create_pack(sql, alice, "alice-public")
        set_visibility(
            sql,
            Principal(user_id=alice, is_local=False, disabled=False),
            "alice-public",
            "public-read",
        )
        bob_principal = Principal(user_id=bob, is_local=False, disabled=False)
        with pytest.raises(PackForbiddenError):
            assert_writable(sql, bob_principal, "alice-public")


class TestSetVisibility:
    def test_owner_can_change_visibility(self, sql, alice):
        create_pack(sql, alice, "mine")
        principal = Principal(user_id=alice, is_local=False, disabled=False)
        pack = set_visibility(sql, principal, "mine", "public-fork")
        assert pack["visibility"] == "public-fork"
        assert get_pack(sql, "mine")["visibility"] == "public-fork"

    def test_invalid_visibility_value_rejected(self, sql, alice):
        create_pack(sql, alice, "mine")
        principal = Principal(user_id=alice, is_local=False, disabled=False)
        with pytest.raises(ValueError):
            set_visibility(sql, principal, "mine", "public")  # not a real value

    def test_invalid_visibility_rejected_before_ownership_check(self, sql, alice, bob):
        """A bad value fails the same way for owner and non-owner alike --
        no ownership signal leaks through validation order."""
        create_pack(sql, alice, "alice-secret")
        bob_principal = Principal(user_id=bob, is_local=False, disabled=False)
        with pytest.raises(ValueError):
            set_visibility(sql, bob_principal, "alice-secret", "not-a-real-value")

    def test_non_owner_cannot_change_visibility(self, sql, alice, bob):
        create_pack(sql, alice, "mine")
        bob_principal = Principal(user_id=bob, is_local=False, disabled=False)
        with pytest.raises(PackNotFoundError):
            set_visibility(sql, bob_principal, "mine", "public-read")
        assert get_pack(sql, "mine")["visibility"] == "private"


# ---------------------------------------------------------------------------
# MCP tool level: pack_create's collision handling + pack_publish
# ---------------------------------------------------------------------------


def _base_ctx(sql, **overrides):
    builder = MagicMock()
    # pack_create's compensating-delete branch (#146 A) requires positive
    # confirmation the anchor write landed in graph -- a bare MagicMock()
    # return isn't a dict, so store_write_succeeded() would read it as "no".
    builder.add_node.return_value = {"stores": {"graph": "ok"}}
    ctx = {
        "neo4j": MagicMock(),
        "chroma": MagicMock(),
        "mongo": MagicMock(),
        "sql": sql,
        "builder": builder,
        "rebac": MagicMock(),
        "impact": MagicMock(),
        "hybrid": MagicMock(),
        "billing": MagicMock(),
    }
    # #146 P1(a): identity-ownership probes (pack_create's anchor-node
    # check) -- see tests/test_tools_handlers_direct.py's _base_ctx for why
    # these must be explicit ("no conflicting store" is this suite's
    # original implicit assumption; a bare MagicMock lookup returns a
    # truthy non-dict, which the probe treats as fail-closed/unverifiable).
    ctx["neo4j"].get_node.return_value = None
    ctx["neo4j"].get_node_by_id.return_value = None
    ctx["neo4j"].get_edge.return_value = None
    ctx["neo4j"].lookup_node_type.return_value = None
    ctx["mongo"].get_node_doc.return_value = None
    ctx["mongo"].get_source.return_value = None
    ctx["chroma"].get_by_id.return_value = None
    ctx.update(overrides)
    return ctx


class TestPackCreateCollision:
    def test_two_users_same_title_both_succeed_no_cross_leak(self, sql):
        """#146 required test: two different users creating a pack with the
        SAME title both succeed, and neither response reveals the other's
        pack exists."""
        from opencrab.mcp.tools import pack_create

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(sql)
            with principal_scope(Principal(user_id="alice", is_local=True, disabled=False)):
                result_a = pack_create(title="My Notes", pack_id="my-notes")
            with principal_scope(Principal(user_id="bob", is_local=True, disabled=False)):
                result_b = pack_create(title="My Notes", pack_id="my-notes")

        assert "error" not in result_a
        assert "error" not in result_b
        assert result_a["pack_id"] == "my-notes"
        # #146 B: random suffix on collision, not sequential -2 -- shape,
        # not exact value.
        assert result_b["pack_id"] != "my-notes"
        assert result_b["pack_id"].startswith("my-notes-")
        assert result_b["pack_id"] != "my-notes-2"
        # Neither response contains an "already exists"-style message or any
        # hint that a colliding pack exists.
        assert "hint" not in result_a
        assert "hint" not in result_b


class TestPackPublish:
    def test_owner_can_publish(self, sql):
        from opencrab.mcp.tools import dispatch_tool

        create_pack(sql, "alice", "alice-pack")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(sql)
            with principal_scope(Principal(user_id="alice", is_local=True, disabled=False)):
                result = dispatch_tool(
                    "pack_publish", {"pack_id": "alice-pack", "visibility": "public-read"}
                )
        assert result == {"status": "ok", "pack_id": "alice-pack", "visibility": "public-read"}
        assert get_pack(sql, "alice-pack")["visibility"] == "public-read"

    def test_non_owner_publish_rejected(self, sql):
        from opencrab.mcp.tools import dispatch_tool

        create_pack(sql, "alice", "alice-pack")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(sql)
            with principal_scope(Principal(user_id="bob", is_local=True, disabled=False)):
                result = dispatch_tool(
                    "pack_publish", {"pack_id": "alice-pack", "visibility": "public-read"}
                )
        assert "error" in result
        assert get_pack(sql, "alice-pack")["visibility"] == "private"

    def test_invalid_visibility_rejected(self, sql):
        from opencrab.mcp.tools import dispatch_tool

        create_pack(sql, "alice", "alice-pack")
        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(sql)
            with principal_scope(Principal(user_id="alice", is_local=True, disabled=False)):
                result = dispatch_tool(
                    "pack_publish", {"pack_id": "alice-pack", "visibility": "public"}
                )
        assert "error" in result
        assert get_pack(sql, "alice-pack")["visibility"] == "private"

    def test_nonexistent_pack_publish_rejected(self, sql):
        from opencrab.mcp.tools import dispatch_tool

        with patch("opencrab.mcp.tools._get_context") as mock_ctx:
            mock_ctx.return_value = _base_ctx(sql)
            with principal_scope(Principal(user_id="alice", is_local=True, disabled=False)):
                result = dispatch_tool(
                    "pack_publish", {"pack_id": "no-such-pack", "visibility": "public-read"}
                )
        assert result == {"error": "pack not found", "pack_id": "no-such-pack"}
