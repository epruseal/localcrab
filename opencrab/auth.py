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
    """Insert a new user row and its default pack, in ONE transaction.
    Returns the generated user_id.

    Raises the underlying driver's IntegrityError if ``is_local=True`` and a
    local user already exists -- ``idx_users_single_local`` (sql_store.py)
    enforces at most one at the DB level, so there's nothing to check here.

    #148: every user needs a default pack to write into when a write omits
    ``pack_id`` (see ``opencrab.pack.ownership.ensure_default_pack``), and
    ``packs.owner_id`` FK-references ``users.user_id`` -- so the pack insert
    must happen AFTER the user row lands, and in the SAME transaction: a
    crash between two separate transactions would leave a user with no
    default pack that ``resolve_write_pack`` could hand out (the "legacy
    user" case is recovered lazily there, but there's no reason to create
    that state on the happy path).
    """
    from sqlalchemy import text

    from opencrab.pack.ownership import ensure_default_pack

    user_id = f"user_{uuid.uuid4().hex[:12]}"
    with sql._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (user_id, display_name, is_local) "
                "VALUES (:user_id, :display_name, :is_local)"
            ),
            {"user_id": user_id, "display_name": display_name, "is_local": is_local},
        )
        ensure_default_pack(sql, user_id, conn=conn)
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
    sql: Any,
    *,
    display_name: str = "local",
    token_name: str | None = "bootstrap",
    issue_token: bool = True,
) -> tuple[str, str | None]:
    """Create the local user, its default pack, and (by default) issue its
    first token, all in ONE transaction. Returns ``(user_id, secret)`` --
    *secret* is ``None`` when ``issue_token=False``.

    Used by ``opencrab init``'s bootstrap path (see cli.py's
    ``_bootstrap_local_user``, default ``issue_token=True``) and by the #245
    stdio auto-bootstrap path (``bootstrap_local_user_idempotent``, called
    with ``issue_token=False`` -- stdio's trust boundary is the OS process,
    so no token is ever readable, and issuing one would leave an unusable
    secret's hash in the DB, see design #245 §3.3).

    ``create_user`` + ``issue_token`` run as two separate transactions, so a
    crash between them could leave a local user with no token that could
    ever verify -- and since ``idx_users_single_local`` caps ``is_local=1``
    at one row, that user could never be recreated either. Wrapping every
    insert in a single ``begin()`` means a failure of any one rolls back all
    of them, leaving no partial state.

    #148: the default-pack insert is here (not a call to ``create_user``,
    which would open its own transaction) for the same all-or-nothing reason
    -- see ``create_user``'s docstring for why the pack must exist before
    any write can omit ``pack_id``, and after the users row (FK).
    """
    from sqlalchemy import text

    from opencrab.pack.ownership import ensure_default_pack

    user_id = f"user_{uuid.uuid4().hex[:12]}"
    secret: str | None = None
    with sql._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (user_id, display_name, is_local) "
                "VALUES (:user_id, :display_name, :is_local)"
            ),
            {"user_id": user_id, "display_name": display_name, "is_local": True},
        )
        ensure_default_pack(sql, user_id, conn=conn)
        if issue_token:
            secret = _TOKEN_PREFIX + secrets.token_urlsafe(32)
            token_id = f"tok_{uuid.uuid4().hex[:12]}"
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
        "creates the local user and issues its first token. (Looked for a "
        f"local user under {settings.local_data_dir!r}. If this process is "
        "a stdio MCP server with an explicit LOCAL_DATA_DIR, set "
        "OPENCRAB_BOOTSTRAP_ON_EMPTY=1 to bootstrap it automatically "
        "instead of running 'opencrab init' by hand -- see #245.)"
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


# ---------------------------------------------------------------------------
# Opt-in stdio auto-bootstrap (#245)
#
# Off by default -- see require_local_principal's docstring for why the
# default path CREATES NOTHING. This section exists only to let an Agent
# Plugin's stdio launch (mcp.json already pins LOCAL_DATA_DIR explicitly)
# skip the manual `opencrab init` step, gated behind an opt-in env var and a
# stack of layered checks (design #245 v13 §3.1-§3.2) so it can never turn
# into the 2026-07-07 incident's shape: a missing/blank config silently
# falling back to a built-in default path and serving an empty store.
# ---------------------------------------------------------------------------

_NOT_FOUND = object()  # sentinel: no local user yet, caller should proceed


def bootstrap_on_empty_requested() -> bool:
    """G1: parse ``OPENCRAB_BOOTSTRAP_ON_EMPTY``.

    unset / "" / "0" -> False (off, current pre-#245 behaviour, unchanged).
    "1" -> True. Anything else is a loud RuntimeError -- a mistyped value
    (``"true"``, ``"yes"``, a stray leading space) must not be silently
    treated as off, the way ``MCP_PROTOCOL_VERSIONS``-style flags are parsed
    elsewhere in this codebase.
    """
    raw = os.environ.get("OPENCRAB_BOOTSTRAP_ON_EMPTY")
    if raw is None or raw in ("", "0"):
        return False
    if raw == "1":
        return True
    raise RuntimeError(
        "OPENCRAB_BOOTSTRAP_ON_EMPTY must be unset, \"\", \"0\", or \"1\" -- "
        f"got {raw!r}."
    )


def _probe_local_user(sql: Any) -> Principal | None:
    """``get_local_user``, but a missing ``users`` table reads as "no user
    yet" (the store exists but was never bootstrapped, or a prior bootstrap
    crashed before DDL landed) instead of propagating -- the same exception
    ``require_local_principal`` tolerates for the same reason."""
    try:
        return get_local_user(sql)
    except Exception as exc:  # noqa: BLE001
        if _looks_like_missing_table(exc):
            return None
        raise


def _close_store(sql: Any) -> None:
    """Best-effort close, mirroring the ``getattr(..., "close", None)``
    idiom cli.py's ``serve`` command already uses for its startup stores --
    ``SQLStore`` has no ``close()`` today, so this is currently a no-op, but
    stays defensive against a future one the way that call site already is.
    """
    close = getattr(sql, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def _discover_local_user(sql: Any) -> Any:
    """One no-create-DDL discovery attempt against an already-open store.

    Returns a ``Principal`` for an enabled local user; ``None`` if the store
    failed to open or the found user is disabled (both must delegate to
    ``require_local_principal``'s authoritative diagnostics rather than
    bootstrap proceeding); or the ``_NOT_FOUND`` sentinel if nothing exists
    yet and the caller should proceed toward the locked creation stage.
    """
    if not getattr(sql, "available", False):
        return None
    principal = _probe_local_user(sql)
    if principal is None:
        return _NOT_FOUND
    return principal if not principal.disabled else None


def bootstrap_local_user_idempotent(
    sql: Any, *, issue_token: bool = True
) -> tuple[str, str | None, bool]:
    """Create the local user if one doesn't exist yet, converging concurrent
    creators onto a single row. Returns ``(user_id, secret, created)`` --
    *secret* is ``None`` whenever no token was issued (``issue_token=False``,
    or a concurrent creator won the race), and *created* is True only for
    the caller that actually inserted the row.

    Extracted from cli.py's ``_bootstrap_local_user`` (#144) so both it and
    the #245 stdio auto-bootstrap path (``maybe_bootstrap_on_empty``, which
    also holds ``bootstrap.lock`` around this call) share one IntegrityError
    convergence rule instead of two copies drifting apart. The lock makes
    the race this handles rare in the stdio path, but callers such as
    ``opencrab init`` against PostgreSQL invoke this with no lock held at
    all, so the convergence logic must stand on its own.
    """
    from sqlalchemy.exc import IntegrityError

    existing = get_local_user(sql)
    if existing is not None:
        return existing.user_id, None, False

    try:
        user_id, secret = bootstrap_local_user(sql, issue_token=issue_token)
    except IntegrityError as exc:
        # Dialects report this differently: PostgreSQL names the index
        # ("idx_users_single_local"), SQLite names the column instead
        # ("users.is_local", confirmed by direct reproduction) -- match
        # either. Any other IntegrityError is a real failure and must
        # propagate.
        orig = str(exc.orig)
        if "idx_users_single_local" not in orig and "users.is_local" not in orig:
            raise
        winner = get_local_user(sql)
        if winner is None:
            raise RuntimeError(
                "Local user bootstrap race detected, but no local user "
                "exists afterward -- something else is wrong."
            ) from exc
        return winner.user_id, None, False

    return user_id, secret, True


def maybe_bootstrap_on_empty() -> Principal | None:
    """Opt-in auto-bootstrap for the two stdio entry points (design #245
    v13). Returns a ``Principal`` when a local user is found or created,
    ``None`` when the opt-in is off or the fast path must delegate to
    ``require_local_principal`` for its authoritative diagnostic -- callers
    wire this as ``principal = maybe_bootstrap_on_empty() or
    require_local_principal()``.

    G1-G4 (§3.1) gate every side effect: opt-in must be exactly "1"
    (``bootstrap_on_empty_requested``), storage mode must be exactly
    "local" (narrower than ``settings.is_local``, which also allows
    "kuzu" -- the plugin contract only ever pins a local sqlite store),
    ``LOCAL_DATA_DIR`` must be explicitly set and non-blank, must not
    contain "?" (sqlalchemy's sqlite URL parsing truncates there, splitting
    the checked/locked path from the connected one), and must already
    exist as a directory (this function never creates one -- PLUGIN_DATA's
    existence is the client's contract).

    Step 0 (§3.2): before ever taking the lock, mirror
    ``require_local_principal``'s own no-create precaution (the same
    ``is_file()`` precheck, since even ``create_tables=False`` still
    connects and thus creates a missing SQLite file as a side effect) so a
    pristine directory causes zero filesystem writes when nothing needs
    bootstrapping, and a lock-acquisition failure afterward truly leaves
    the directory untouched. An already-bootstrapped enabled user returns
    its ``Principal`` directly here with the same one-store/one-query cost
    as calling ``require_local_principal`` alone.
    """
    if not bootstrap_on_empty_requested():
        return None

    import sys
    from contextlib import ExitStack
    from pathlib import Path

    from opencrab.config import get_settings
    from opencrab.locking import file_lock
    from opencrab.stores.sql_store import SQLStore

    settings = get_settings()

    # G2: exactly "local" -- narrower than settings.is_local (also true for
    # "kuzu"), since the plugin contract pins a local sqlite store only.
    if settings.storage_mode != "local":
        raise RuntimeError(
            "OPENCRAB_BOOTSTRAP_ON_EMPTY requires STORAGE_MODE=local -- got "
            f"{settings.storage_mode!r}. Automatic bootstrap only targets "
            "the plugin's own local SQLite store."
        )

    # G3: LOCAL_DATA_DIR must be an explicit, non-blank, '?'-free source.
    if "local_data_dir" not in settings.model_fields_set:
        raise RuntimeError(
            "OPENCRAB_BOOTSTRAP_ON_EMPTY requires LOCAL_DATA_DIR to be set "
            "explicitly -- refusing to auto-bootstrap into a built-in "
            "default path. Run 'opencrab init' or set LOCAL_DATA_DIR."
        )
    data_dir = settings.local_data_dir
    if not data_dir.strip():
        raise RuntimeError(
            "OPENCRAB_BOOTSTRAP_ON_EMPTY requires a non-blank LOCAL_DATA_DIR."
        )
    if "?" in data_dir:
        raise RuntimeError(
            f"LOCAL_DATA_DIR ({data_dir!r}) contains '?', which SQLAlchemy's "
            "sqlite URL parsing truncates at -- refusing to auto-bootstrap "
            "into an ambiguous path."
        )

    # G4: the directory itself must already exist -- never created here.
    if not Path(data_dir).is_dir():
        raise RuntimeError(
            f"LOCAL_DATA_DIR ({data_dir}) does not exist. Automatic "
            "bootstrap never creates directories, only the database inside "
            "one that already exists."
        )

    db_path = Path(data_dir, "opencrab.db")

    # Step 0: no-create, no-lock fast path.
    if db_path.is_file():
        sql = SQLStore(settings.sqlite_url, create_tables=False)
        try:
            outcome = _discover_local_user(sql)
        finally:
            _close_store(sql)
        if outcome is not _NOT_FOUND:
            return outcome  # Principal, or None to delegate.

    with ExitStack() as stack:
        # Step 1: acquire the cross-process creation lock. Only ACQUISITION
        # failures convert to RuntimeError -- a body failure (the `try`
        # covers just `enter_context`) must propagate unconverted, or a real
        # bug inside the critical section gets misdiagnosed as "couldn't get
        # the lock".
        try:
            stack.enter_context(file_lock("bootstrap.lock", data_dir=data_dir, timeout=30))
        except (TimeoutError, OSError) as exc:
            raise RuntimeError(
                "Could not acquire the automatic-bootstrap lock "
                f"(bootstrap.lock) under {data_dir} within 30s -- another "
                "process may be holding it, or the lock file could not be "
                "opened."
            ) from exc

        # Step 2: recheck immediately inside the lock -- another process may
        # have finished bootstrapping while this one waited.
        probe_sql = SQLStore(settings.sqlite_url, create_tables=False)
        try:
            outcome = _discover_local_user(probe_sql)
        finally:
            _close_store(probe_sql)
        if outcome is not _NOT_FOUND:
            return outcome

        # Step 3: open with DDL (CREATE IF NOT EXISTS is idempotent) and
        # recheck once more before creating anything.
        sql = SQLStore(settings.sqlite_url, create_tables=True)
        try:
            if not getattr(sql, "available", False):
                raise RuntimeError(
                    "Automatic bootstrap could not open the SQL store at "
                    f"{data_dir} after creating tables -- this is a "
                    "store-open failure, not a missing 'opencrab init'."
                )

            principal = _probe_local_user(sql)
            if principal is not None:
                return principal if not principal.disabled else None

            # Step 4: create -- no token, stdio's trust boundary is the
            # process (#145, design #245 §3.3).
            user_id, _secret, created = bootstrap_local_user_idempotent(
                sql, issue_token=False
            )

            # Step 5: stderr-only creation notice, gated on created=True --
            # a helper that converged onto a concurrent creator's row
            # (created=False) must print nothing new.
            if created:
                print(
                    f"opencrab: auto-bootstrapped local user {user_id} at "
                    f"{data_dir}",
                    file=sys.stderr,
                )

            # Step 6: final recheck through the same enabled test as every
            # other discovery branch -- an external process could have
            # disabled the just-created user between the helper's commit and
            # this query, and a truthy Principal here would let it slip past
            # require_local_principal's authoritative disabled check.
            final = _probe_local_user(sql)
            if final is None:
                return None
            return final if not final.disabled else None
        finally:
            _close_store(sql)
