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
import os
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

_TOKEN_PREFIX = "lc_"

# Pre-#145 shared-secret env vars. None of them configure anything anymore --
# the shared-secret auth model they fed (opencrab/mcp/http_app.py's old
# ``auth_token`` param, apps/api/main.py's OPENCRAB_API_KEY) is deleted. A
# leftover value would make an operator believe a deployment is still gated
# when it isn't, which is worse than no env var at all.
_STALE_SECRET_ENV_VARS = ("OPENCRAB_API_KEY", "LOCALCRAB_MCP_TOKEN", "LOCALCRAB_MCP_TOKEN_FILE")


def refuse_stale_shared_secret_env() -> None:
    """Raise if a pre-#145 shared-secret env var is still set.

    Lives here (not ``opencrab.mcp.http_app`` or ``opencrab.mcp.server``) so
    every process entry point that must call it can, without a circular
    import: ``opencrab.mcp.http_app`` imports ``opencrab.mcp.server``
    (``MCPServer``), so ``server.py`` cannot import back from
    ``http_app.py``. ``opencrab.auth`` depends on neither, so both -- plus
    ``opencrab/cli.py`` and ``apps/api/main.py`` -- import it from here.
    ``http_app`` re-exports the name for backward compatibility with
    existing imports/tests.
    """
    stale = [name for name in _STALE_SECRET_ENV_VARS if os.environ.get(name, "").strip()]
    if stale:
        raise RuntimeError(
            "Refusing to start: stale shared-secret env var(s) set: "
            f"{', '.join(stale)}. These no longer configure authentication "
            "(the shared-secret model was deleted in #145) -- per-user "
            "tokens issued via 'opencrab token issue' are the only auth "
            "mechanism now.\n"
            "Found in this process's environment. If you did not export it "
            "yourself, check whatever launched the process (a docker-compose "
            "environment: entry, a systemd unit's Environment=, a wrapper "
            "script) -- and, when running apps/api, the repository root .env "
            "or apps/.env, which that entry point loads into the environment "
            "at import time (#88). Other entry points do not read those "
            "files into the environment, so a value left only in a .env "
            "file is not what triggered this."
        )


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


# ---------------------------------------------------------------------------
# Process entry-point guards (#145)
#
# These live here rather than in cli.py or mcp/server.py because BOTH of those
# need them and mcp/http_app.py already imports mcp/server.py -- putting the
# shared logic in either would make the import graph circular. They are also
# the reason `python -m opencrab.mcp.server` is not a hole: that entry point
# calls the same two functions cli.py's `serve --transport stdio` does.
# ---------------------------------------------------------------------------

def require_local_principal() -> Principal:
    """Return the enabled local-user Principal, or raise with what to do.

    stdio has no per-request identity: the transport's trust boundary is the
    OS process, and the server acts as exactly one local user for its whole
    lifetime. Both stdio entry points bind this, and so does every CLI command
    that writes on the user's behalf.

    CREATES NOTHING. A caller that has no local user must fail leaving the data
    directory exactly as it found it -- refusing a write and then leaving a
    freshly created database file behind is its own small lie. That takes two
    precautions, because either alone is insufficient:

    - ``make_sql_store`` builds ``SQLStore`` with its default
      ``create_tables=True``, so it would run DDL. Hence ``create_tables=False``.
    - On SQLite that is still not enough: *connecting* to a path that does not
      exist creates the file. Hence the ``is_file()`` precheck, which is only
      meaningful where the store is a local file.

    ``settings.is_local`` is ``local`` and ``kuzu``. The other two modes,
    ``docker`` and ``pg``, both point ``make_sql_store`` at PostgreSQL, where
    there is no file to check -- they connect (without DDL) and read.

    A missing ``users`` table is treated as "not bootstrapped", the same as a
    missing file. A *connection* failure is not: swallowing it as "run init"
    would send an operator chasing the wrong problem.
    """
    from pathlib import Path

    from opencrab.config import get_settings
    from opencrab.stores.sql_store import SQLStore

    settings = get_settings()
    not_bootstrapped = RuntimeError(
        "No local user is bootstrapped. Run 'opencrab init' first -- it "
        "creates the local user and issues its first token."
    )

    if settings.is_local:
        # File first: on SQLite, *connecting* to a missing path creates it.
        if not Path(settings.local_data_dir, "opencrab.db").is_file():
            raise not_bootstrapped
        url = settings.sqlite_url
    else:
        # docker and pg both mean PostgreSQL. No file to check, and
        # `make_sql_store` is not used here because it would take SQLStore's
        # default create_tables=True and run DDL -- this function must not.
        url = settings.postgres_url

    sql = SQLStore(url=url, create_tables=False)

    if not getattr(sql, "available", False):
        # SQLStore swallows the connect error into a log line and a False
        # flag, so the reason is not retrievable here. What IS known: the
        # local file existed (checked above) or this is a remote database.
        # Either way the store failed to open, which is not the same thing as
        # "you have not run init" -- reporting it as that would send an
        # operator to fix the wrong problem.
        raise RuntimeError(
            "Could not open the SQL store. This is not a missing bootstrap -- "
            "the database exists (or is remote) but did not connect. Check the "
            "preceding 'SQL store unavailable' log line for the driver error, "
            "then the file permissions or the PostgreSQL connection settings."
        )

    try:
        principal = get_local_user(sql)
    except Exception as exc:  # noqa: BLE001
        # Distinguish "no users table yet" (not bootstrapped) from a real
        # query/driver failure, which must surface as itself.
        if _looks_like_missing_table(exc):
            raise not_bootstrapped from exc
        raise

    if principal is None:
        raise not_bootstrapped
    if principal.disabled:
        raise RuntimeError(
            f"The local user ({principal.user_id}) is disabled. Re-enable it "
            f"with 'opencrab user enable {principal.user_id}'."
        )
    return principal


def _looks_like_missing_table(exc: Exception) -> bool:
    """True when *exc* says the ``users`` table does not exist.

    Dialect-specific wording, matched loosely on purpose: SQLite says
    "no such table: users", PostgreSQL says 'relation "users" does not exist'.
    Getting this wrong in the permissive direction would hide a connection
    failure behind "run init", so the match requires the table name.
    """
    text = str(getattr(exc, "orig", exc)).lower()
    if "users" not in text:
        return False
    return "no such table" in text or "does not exist" in text or "undefined table" in text
