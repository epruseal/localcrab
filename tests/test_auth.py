"""Unit tests for opencrab.auth (#144).

Mirrors tests/test_stores.py::TestSQLStoreUnit's style: SQLStore("sqlite:///:memory:")
needs no LOCAL_DATA_DIR/scratch dir at all, so these run fully in-memory.
"""

from __future__ import annotations

import pytest

from opencrab.auth import (
    Principal,
    bootstrap_local_user,
    create_user,
    current_principal,
    disable_user,
    enable_user,
    get_local_user,
    hash_token,
    issue_token,
    list_tokens,
    list_users,
    principal_scope,
    revoke_token,
    verify_token,
)


@pytest.fixture
def sql():
    from opencrab.stores.sql_store import SQLStore

    return SQLStore("sqlite:///:memory:")


class TestUsers:
    def test_create_user_returns_id(self, sql):
        user_id = create_user(sql, "Alice")
        assert user_id
        assert user_id in {u["user_id"] for u in list_users(sql)}

    def test_second_local_user_violates_constraint(self, sql):
        create_user(sql, "Local One", is_local=True)
        with pytest.raises(Exception):
            create_user(sql, "Local Two", is_local=True)

    def test_get_local_user_none_when_absent(self, sql):
        assert get_local_user(sql) is None

    def test_get_local_user_returns_principal(self, sql):
        user_id = create_user(sql, "Local", is_local=True)
        principal = get_local_user(sql)
        assert principal == Principal(user_id=user_id, is_local=True, disabled=False)

    def test_get_local_user_reflects_disabled_state(self, sql):
        """get_local_user does NOT filter on disabled -- a disabled local
        row still occupies idx_users_single_local's slot, so callers must
        still see it (via Principal.disabled) or they'd try to recreate it
        and hit the unique index."""
        user_id = create_user(sql, "Local", is_local=True)
        disable_user(sql, user_id)
        principal = get_local_user(sql)
        assert principal == Principal(user_id=user_id, is_local=True, disabled=True)

    def test_disable_user(self, sql):
        user_id = create_user(sql, "Alice")
        assert disable_user(sql, user_id) is True
        assert [u for u in list_users(sql) if u["user_id"] == user_id][0]["disabled"] is True

    def test_disable_unknown_user_returns_false(self, sql):
        assert disable_user(sql, "nope") is False

    def test_enable_user_round_trip(self, sql):
        user_id = create_user(sql, "Alice")
        disable_user(sql, user_id)
        assert enable_user(sql, user_id) is True
        assert [u for u in list_users(sql) if u["user_id"] == user_id][0]["disabled"] is False

    def test_enable_unknown_user_returns_false(self, sql):
        assert enable_user(sql, "nope") is False

    def test_disable_then_enable_local_user_then_init_does_not_duplicate(self, sql):
        """disable -> enable round-trips on the local user, and a bootstrap
        in between (mirroring cli.py's init check) must not create a second
        local user."""
        user_id, _ = bootstrap_local_user(sql)
        disable_user(sql, user_id)

        # init-style check: an existing local user (disabled or not) means
        # do not bootstrap again.
        assert get_local_user(sql) is not None

        enable_user(sql, user_id)
        assert get_local_user(sql) == Principal(user_id=user_id, is_local=True, disabled=False)
        assert len([u for u in list_users(sql) if u["is_local"]]) == 1

    def test_bootstrap_local_user_atomic_on_token_issuance_failure(self, sql, monkeypatch):
        """create_user + issue_token run in ONE transaction inside
        bootstrap_local_user (#144 fix design 3a): a failure while issuing
        the token must roll back the user insert too, leaving no orphan
        tokenless local user."""
        import opencrab.auth as auth_module

        def boom(secret):
            raise RuntimeError("simulated token-issuance failure")

        monkeypatch.setattr(auth_module, "hash_token", boom)

        with pytest.raises(RuntimeError):
            bootstrap_local_user(sql)

        assert list_users(sql) == []


class TestTokens:
    def test_verify_issued_token_returns_principal(self, sql):
        user_id = create_user(sql, "Alice")
        _, secret = issue_token(sql, user_id, name="test")
        principal = verify_token(sql, secret)
        assert principal == Principal(user_id=user_id, is_local=False, disabled=False)

    def test_verify_issued_token_principal_disabled_always_false(self, sql):
        """verify_token's WHERE clause already excludes disabled owners, so
        the returned Principal.disabled must always be False by
        construction -- never read off a (possibly contaminated) row."""
        user_id = create_user(sql, "Local", is_local=True)
        _, secret = issue_token(sql, user_id)
        principal = verify_token(sql, secret)
        assert principal.disabled is False

    def test_verify_revoked_token_returns_none(self, sql):
        user_id = create_user(sql, "Alice")
        token_id, secret = issue_token(sql, user_id)
        revoke_token(sql, token_id)
        assert verify_token(sql, secret) is None

    def test_verify_token_of_disabled_user_returns_none(self, sql):
        user_id = create_user(sql, "Alice")
        _, secret = issue_token(sql, user_id)
        disable_user(sql, user_id)
        assert verify_token(sql, secret) is None

    def test_verify_unknown_token_returns_none(self, sql):
        assert verify_token(sql, "lc_does-not-exist") is None

    @pytest.mark.parametrize("bad_input", [None, "", "   ", "\t\n"])
    def test_verify_token_returns_none_for_falsy_input(self, sql, bad_input):
        """Previously raised AttributeError from hash_token()'s .encode()
        call on a non-string / empty presented token."""
        assert verify_token(sql, bad_input) is None

    def test_issue_token_fails_for_unknown_user(self, sql):
        with pytest.raises(ValueError):
            issue_token(sql, "user_does_not_exist")
        from sqlalchemy import text

        with sql._engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM api_tokens")).fetchone()[0]
        assert count == 0

    def test_issue_token_fails_for_disabled_user(self, sql):
        user_id = create_user(sql, "Alice")
        disable_user(sql, user_id)
        with pytest.raises(ValueError):
            issue_token(sql, user_id)
        from sqlalchemy import text

        with sql._engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM api_tokens")).fetchone()[0]
        assert count == 0

    def test_list_tokens_excludes_hash_and_secret(self, sql):
        user_id = create_user(sql, "Alice")
        _, secret = issue_token(sql, user_id, name="mine")
        tokens = list_tokens(sql, user_id)
        assert len(tokens) == 1
        assert tokens[0]["name"] == "mine"
        assert "token_hash" not in tokens[0]
        assert secret not in str(tokens[0])

    def test_no_plaintext_secret_anywhere_in_api_tokens(self, sql):
        """Acceptance criterion (#144): dump every column of api_tokens and
        grep for the issued secret string. Expect 0 hits."""
        from sqlalchemy import text

        user_id = create_user(sql, "Alice")
        _, secret = issue_token(sql, user_id)
        with sql._engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM api_tokens")).fetchall()
        dump = str([dict(r._mapping) for r in rows])
        assert secret not in dump

    def test_hash_token_is_sha256_hex(self):
        digest = hash_token("lc_abc")
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


def _insert_contaminated_user(sql, user_id: str, *, is_local: int, disabled: int = 0) -> None:
    """Insert a users row with an is_local/disabled value outside {0, 1},
    bypassing the CHECK constraint via SQLite's documented
    ``ignore_check_constraints`` pragma. Simulates a row that predates
    #144's CHECK (or reached the table through some other path that
    bypassed it) -- the scenario the strict ``== 1`` comparisons in auth.py
    exist to tolerate."""
    from sqlalchemy import text

    with sql._engine.begin() as conn:
        conn.execute(text("PRAGMA ignore_check_constraints = 1"))
        conn.execute(
            text(
                "INSERT INTO users (user_id, display_name, is_local, disabled) "
                "VALUES (:uid, :name, :is_local, :disabled)"
            ),
            {"uid": user_id, "name": "Contaminated", "is_local": is_local, "disabled": disabled},
        )
        conn.execute(text("PRAGMA ignore_check_constraints = 0"))


class TestContamination:
    """A row with is_local/disabled outside {0, 1} (legacy data predating
    #144's CHECK) must not be misread as True by a loose ``bool()``
    conversion -- see auth.py's strict ``== 1`` comparisons."""

    def test_is_local_2_not_treated_as_local_by_get_local_user(self, sql):
        _insert_contaminated_user(sql, "user_bad", is_local=2)
        assert get_local_user(sql) is None

    def test_is_local_2_not_treated_as_local_by_list_users(self, sql):
        _insert_contaminated_user(sql, "user_bad", is_local=2)
        row = [u for u in list_users(sql) if u["user_id"] == "user_bad"][0]
        assert row["is_local"] is False

    def test_is_local_2_not_treated_as_local_by_verify_token(self, sql):
        _insert_contaminated_user(sql, "user_bad", is_local=2)
        _, secret = issue_token(sql, "user_bad")
        principal = verify_token(sql, secret)
        assert principal.is_local is False


class TestPrincipalScope:
    def test_current_principal_raises_outside_scope(self):
        with pytest.raises(LookupError):
            current_principal()

    def test_current_principal_returns_value_inside_scope(self):
        p = Principal(user_id="user_x", is_local=True, disabled=False)
        with principal_scope(p):
            assert current_principal() == p

    def test_current_principal_unset_after_scope_exits(self):
        p = Principal(user_id="user_x", is_local=True, disabled=False)
        with principal_scope(p):
            pass
        with pytest.raises(LookupError):
            current_principal()
