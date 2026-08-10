"""
Verified-principal storage and utilities (#144 -- execution 1 of #143's
auth/authorization design).

This module is the only owner of a server-derived ``Principal``: the
``users`` / ``api_tokens`` tables (defined in ``opencrab/stores/sql_store.py``
next to ``rebac_policies``), token issuance/verification, and the
``contextvars``-based propagation of "who is calling right now".

Only ``opencrab/cli.py`` calls this module (``init``'s bootstrap and the
``user`` / ``token`` command groups). No request-handling surface does:
authentication *enforcement* -- rejecting unauthenticated MCP/REST calls and
deriving a Principal from a request -- is #145. This PR builds the storage
and primitives plus the CLI to administer them.

ContextVar 전파 범위 (읽어야 하는 주의사항): ``current_principal()`` /
``principal_scope()`` 는 ``contextvars.ContextVar`` 하나로 구현된다. 같은
코루틴이 만든 ``await`` 체인 안에서는 값이 그대로 보인다 -- Python
contextvars 는 코루틴 실행 컨텍스트를 따라가기 때문이다. 하지만
``opencrab/mcp/http_app.py`` 의 async 라우트가 ``opencrab/mcp/server.py``
의 sync ``MCPServer.handle_request`` 를 스레드풀/executor(예:
``asyncio.to_thread``, ``loop.run_in_executor``)로 옮겨 호출하면, 그
경계에서 컨텍스트가 복사되지 않아 설정해둔 principal 이 보이지 않게 된다.
그런 실행 경로에 이 모듈을 연결할 때는 principal 을 인자로 명시적으로
넘기거나, 스레드 진입 시 ``contextvars.copy_context()`` 로 복사해야 한다.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

_TOKEN_PREFIX = "lc_"


@dataclass(frozen=True)
class Principal:
    """A verified caller identity, derived server-side from a token (HTTP)
    or the local stdio/CLI user binding -- never a client-supplied field
    (see #143 "principal" definition)."""

    user_id: str
    is_local: bool
    disabled: bool


# ---------------------------------------------------------------------------
# Token hashing
# ---------------------------------------------------------------------------


def hash_token(secret: str) -> str:
    """sha256 hex digest of a presented token secret."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def create_user(sql: Any, display_name: str, is_local: bool = False) -> str:
    """Insert a new user row. Returns the generated user_id.

    Raises the underlying driver's IntegrityError if ``is_local=True`` and a
    local user already exists -- ``idx_users_single_local`` (sql_store.py)
    enforces at most one at the DB level, so there's nothing to check here.
    """
    from sqlalchemy import text

    user_id = f"user_{uuid.uuid4().hex[:12]}"
    with sql._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (user_id, display_name, is_local) "
                "VALUES (:user_id, :display_name, :is_local)"
            ),
            {"user_id": user_id, "display_name": display_name, "is_local": is_local},
        )
    return user_id


def get_local_user(sql: Any) -> Principal | None:
    """Return the single is_local=TRUE user, or None if none exists yet.

    Deliberately does NOT filter on ``disabled`` -- a disabled local row
    still occupies ``idx_users_single_local``'s slot, so callers (``init``'s
    bootstrap check) need to see it too, or they'd try to create a second
    local user and hit that unique index. The caller reads
    ``Principal.disabled`` to see the real state.
    """
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id, disabled FROM users WHERE is_local = :is_local"),
            {"is_local": True},
        ).fetchone()
    if row is None:
        return None
    return Principal(user_id=row[0], is_local=True, disabled=row[1] == 1)


def list_users(sql: Any) -> list[dict[str, Any]]:
    """List all users (for ``opencrab user list``)."""
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT user_id, display_name, is_local, disabled, created_at FROM users"
            )
        ).fetchall()
    return [
        {
            "user_id": r[0],
            "display_name": r[1],
            "is_local": r[2] == 1,
            "disabled": r[3] == 1,
            "created_at": str(r[4]) if r[4] is not None else None,
        }
        for r in rows
    ]


def disable_user(sql: Any, user_id: str) -> bool:
    """Set ``disabled=TRUE`` for *user_id*. Returns False if no such user."""
    from sqlalchemy import text

    with sql._engine.begin() as conn:
        result = conn.execute(
            text("UPDATE users SET disabled = :disabled WHERE user_id = :user_id"),
            {"disabled": True, "user_id": user_id},
        )
    return result.rowcount > 0


def enable_user(sql: Any, user_id: str) -> bool:
    """Clear ``disabled`` for *user_id*. Returns False if no such user.

    The recovery path for ``disable_user``: it makes disabling the local
    user safe, since it's always reversible.
    """
    from sqlalchemy import text

    with sql._engine.begin() as conn:
        result = conn.execute(
            text("UPDATE users SET disabled = :disabled WHERE user_id = :user_id"),
            {"disabled": False, "user_id": user_id},
        )
    return result.rowcount > 0


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def issue_token(sql: Any, user_id: str, name: str | None = None) -> tuple[str, str]:
    """Create a new token for *user_id*. Returns ``(token_id, secret)``.

    *secret* is returned to the caller here ONLY -- the store keeps just its
    sha256 hash (``token_hash``); the plaintext is never persisted.

    Raises ``ValueError`` if *user_id* doesn't exist or is disabled, rather
    than inserting a token row that could never verify (#144 fix design).
    """
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        row = conn.execute(
            text("SELECT disabled FROM users WHERE user_id = :user_id"),
            {"user_id": user_id},
        ).fetchone()
    if row is None:
        raise ValueError(f"Unknown user: {user_id}")
    if row[0] == 1:
        raise ValueError(f"User is disabled: {user_id}")

    secret = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = f"tok_{uuid.uuid4().hex[:12]}"
    with sql._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO api_tokens (token_id, user_id, token_hash, name) "
                "VALUES (:token_id, :user_id, :token_hash, :name)"
            ),
            {
                "token_id": token_id,
                "user_id": user_id,
                "token_hash": hash_token(secret),
                "name": name,
            },
        )
    return token_id, secret


def bootstrap_local_user(
    sql: Any, *, display_name: str = "local", token_name: str | None = "bootstrap"
) -> tuple[str, str]:
    """Create the local user and issue its first token in ONE transaction.
    Returns ``(user_id, secret)``.

    Used only by ``opencrab init``'s bootstrap path (see cli.py's
    ``_bootstrap_local_user``): ``create_user`` + ``issue_token`` run as two
    separate transactions, so a crash between them could leave a local user
    with no token that could ever verify -- and since ``idx_users_single_local``
    caps ``is_local=1`` at one row, that user could never be recreated either.
    Wrapping both inserts in a single ``begin()`` means a failure of either
    rolls back both, leaving no partial state.
    """
    from sqlalchemy import text

    user_id = f"user_{uuid.uuid4().hex[:12]}"
    secret = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = f"tok_{uuid.uuid4().hex[:12]}"
    with sql._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (user_id, display_name, is_local) "
                "VALUES (:user_id, :display_name, :is_local)"
            ),
            {"user_id": user_id, "display_name": display_name, "is_local": True},
        )
        conn.execute(
            text(
                "INSERT INTO api_tokens (token_id, user_id, token_hash, name) "
                "VALUES (:token_id, :user_id, :token_hash, :name)"
            ),
            {
                "token_id": token_id,
                "user_id": user_id,
                "token_hash": hash_token(secret),
                "name": token_name,
            },
        )
    return user_id, secret


def verify_token(sql: Any, presented: str) -> Principal | None:
    """Hash *presented* and look it up. Returns a Principal if the token is
    unrevoked and its owner isn't disabled, else None.

    Lookup is a unique-indexed hash-equality match, not a presented-secret
    string compare, so there's no separate timing side-channel to defend
    against (see #144 issue body).

    Returns None (rather than raising) for falsy *presented* -- None, "", or
    whitespace-only -- since ``hash_token`` would otherwise raise
    AttributeError trying to ``.encode()`` a non-string. ``hash_token``
    itself keeps raising on the wrong type; this guard is specific to the
    "caller has no token to present" case.

    Does not update ``last_used_at`` -- there's no caller yet (#145), and
    #143 notes SQLite WAL lets every-request token *reads* avoid contending
    with the write-lock holder; writing here on every call would reopen
    that contention. Wire it up when a real caller needs it.
    """
    if not presented or not presented.strip():
        return None

    from sqlalchemy import text

    with sql._engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT u.user_id, u.is_local FROM api_tokens t "
                "JOIN users u ON u.user_id = t.user_id "
                "WHERE t.token_hash = :token_hash "
                "AND t.revoked_at IS NULL AND u.disabled = :not_disabled"
            ),
            {"token_hash": hash_token(presented), "not_disabled": False},
        ).fetchone()
    if row is None:
        return None
    # This query's WHERE already excludes disabled owners, so the returned
    # Principal's disabled is always False by construction here -- never
    # read off the row (contamination tolerance: an is_local value other
    # than exactly 1 must not be treated as local).
    return Principal(user_id=row[0], is_local=row[1] == 1, disabled=False)


def revoke_token(sql: Any, token_id: str) -> None:
    """Mark a token revoked (sets ``revoked_at``). Idempotent."""
    from sqlalchemy import text

    from opencrab.execution._sql import now_expr

    with sql._engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE api_tokens SET revoked_at = {now_expr(sql)} "
                "WHERE token_id = :token_id AND revoked_at IS NULL"
            ),
            {"token_id": token_id},
        )


def list_tokens(sql: Any, user_id: str) -> list[dict[str, Any]]:
    """List a user's tokens. Never exposes ``token_hash`` or a plaintext
    secret (the latter was never stored)."""
    from sqlalchemy import text

    with sql._engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT token_id, name, created_at, last_used_at, revoked_at "
                "FROM api_tokens WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        ).fetchall()
    return [
        {
            "token_id": r[0],
            "name": r[1],
            "created_at": str(r[2]) if r[2] is not None else None,
            "last_used_at": str(r[3]) if r[3] is not None else None,
            "revoked_at": str(r[4]) if r[4] is not None else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Current-principal propagation
# ---------------------------------------------------------------------------

_current_principal: contextvars.ContextVar[Principal] = contextvars.ContextVar(
    "current_principal"
)


def current_principal() -> Principal:
    """Return the Principal bound by the innermost enclosing
    ``principal_scope()``.

    Raises ``LookupError`` outside any scope -- by design there's no
    "anonymous" Principal (#143: every code path must have a server-derived
    principal or fail, never default to open).
    """
    return _current_principal.get()


@contextlib.contextmanager
def principal_scope(principal: Principal) -> Iterator[None]:
    """Bind *principal* as ``current_principal()`` for the duration of the
    with-block. See the module docstring for the threadpool/executor
    propagation caveat."""
    token = _current_principal.set(principal)
    try:
        yield
    finally:
        _current_principal.reset(token)
