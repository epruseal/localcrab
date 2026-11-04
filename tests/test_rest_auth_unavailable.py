"""#601 -- a token store that is busy or unreachable is not a server fault on REST.

Before the fix, ``require_auth`` (apps/api/main.py) let the store error out of
``verify_token`` unhandled, so a locked ``opencrab.db`` ended every REST request
in a 500, valid token or not. #580 gave the MCP route the 503 + ``Retry-After``
answer for the same situation. These tests pin the same answer on REST and pin
what must not change. See ``docs/issue-601-rest-auth-unavailable.md`` for the
table of tests and the mutations each one must catch.

The tests run the app without its lifespan. They give it a hand-built
``ApiContext`` with a real ``SQLStore`` and doubles for the other stores.
"""
from __future__ import annotations

import inspect
import logging
from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

import opencrab.auth as auth_mod
from apps.api import main as api
from opencrab.auth import bootstrap_local_user
from opencrab.config import get_settings
from opencrab.mcp import http_app as http_mod
from opencrab.stores.sql_store import SQLStore
from tests.test_mcp_auth_unavailable import (
    _busy_error,
    _exclusive_lock,
    _server_error,
    _sqlite_error,
)

# Busy timeout of the test store. Real value: SQLStore.connect_args["timeout"].
_SHORT_BUSY_TIMEOUT = 0.2


class _Env:
    def __init__(self, sql: SQLStore, db_path, secret: str) -> None:
        self.sql = sql
        self.db_path = db_path
        self.secret = secret
        self.client = TestClient(api.app, raise_server_exceptions=False)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    import sqlalchemy

    monkeypatch.setenv("OPENCRAB_TIER", "free")
    real_create_engine = sqlalchemy.create_engine

    def short_timeout(*args, **kwargs):
        connect_args = dict(kwargs.get("connect_args") or {})
        connect_args["timeout"] = _SHORT_BUSY_TIMEOUT
        kwargs["connect_args"] = connect_args
        return real_create_engine(*args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(sqlalchemy, "create_engine", short_timeout)
        sql = SQLStore(f"sqlite:///{tmp_path}/opencrab.db")
    _user_id, secret = bootstrap_local_user(sql)
    ctx = api.ApiContext(
        settings=get_settings(),
        graph=MagicMock(),
        vector=MagicMock(available=False),
        docs=MagicMock(available=False),
        sql=sql,
        hybrid=MagicMock(),
        impact=MagicMock(),
    )
    monkeypatch.setattr(api.app.state, "context", ctx, raising=False)
    monkeypatch.setattr(api, "_UNAVAILABLE_LOGGED", {}, raising=False)
    try:
        yield _Env(sql, tmp_path / "opencrab.db", secret)
    finally:
        sql.close()


def _assert_unavailable(resp) -> None:
    assert resp.status_code == 503, resp.text
    assert resp.headers["retry-after"] == "5"
    assert resp.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in resp.headers
    assert resp.json() == {"detail": "Authentication temporarily unavailable"}


def _raiser(exc: BaseException):
    def _verify(sql, presented):  # noqa: ARG001
        raise exc

    return _verify


def _manifest(env, headers=None):
    return env.client.get("/api/manifest", headers=headers)


# ---------------------------------------------------------------------------
# 1 -- the acceptance criterion: a locked token file is a 503, not a 500
# ---------------------------------------------------------------------------


def test_locked_token_file_is_503_then_recovers(env):
    with _exclusive_lock(env.db_path):
        locked = _manifest(env, env.headers)
    healed = _manifest(env, env.headers)

    _assert_unavailable(locked)
    assert healed.status_code == 200, "the failed lookup left the store unusable"


def test_locked_token_file_is_503_for_an_unverifiable_token_too(env):
    """A locked store cannot judge any token, valid or not."""
    with _exclusive_lock(env.db_path):
        locked = _manifest(env, {"Authorization": "Bearer not-a-token"})
    _assert_unavailable(locked)


# ---------------------------------------------------------------------------
# 2 -- every REST route that authenticates answers the same way
# ---------------------------------------------------------------------------


def _calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _calls(dep)


def _api_routes():
    return [r for r in api.app.routes if isinstance(r, APIRoute) and r.path.startswith("/api/")]


def test_every_api_route_authenticates_through_require_auth():
    """The sibling-path census: a REST route that skips ``require_auth`` would
    skip the 503 contract too."""
    routes = _api_routes()
    assert routes
    # A sync dependency runs in the thread pool, so the wait for a busy store
    # stays off the event loop. An ``async def`` dependency would put that wait on the loop.
    assert not inspect.iscoroutinefunction(api.require_auth)
    bypass = [r.path for r in routes if api.require_auth not in set(_calls(r.dependant))]
    assert bypass == []


def _route_cases():
    return [
        (method, route.path)
        for route in _api_routes()
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
    ]


@pytest.mark.parametrize(("method", "path"), _route_cases())
def test_every_api_route_answers_503_for_a_busy_store(env, monkeypatch, method, path):
    monkeypatch.setattr(auth_mod, "verify_token", _raiser(_busy_error()))
    concrete = path.replace("{", "").replace("}", "")  # path parameters take any text
    resp = env.client.request(method, concrete, headers=env.headers)
    _assert_unavailable(resp)


# ---------------------------------------------------------------------------
# 3 -- what is unavailable (503) and what keeps its earlier answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (_busy_error, 503),
        (lambda: _sqlite_error("database table is locked", 6), 503),
        (lambda: _sqlite_error("database table is locked", 262), 503),  # extended code
        (lambda: _server_error(None), 503),  # driver-side connection failure
        (lambda: _server_error("08006"), 503),
        (lambda: _server_error("57P01"), 503),
        (lambda: _server_error("53300"), 503),
        (lambda: _server_error("28P01", connection_invalidated=True), 503),
        (lambda: PoolTimeoutError("pool exhausted"), 503),
        # Not "temporarily unavailable": the error leaves require_auth as it did.
        (lambda: _sqlite_error("no such table: users", 1), 500),
        (lambda: _sqlite_error("unable to open database file", 14), 500),
        (lambda: _server_error("28P01"), 500),
        (lambda: RuntimeError("anything else"), 500),
    ],
    ids=[
        "sqlite-busy", "sqlite-locked", "sqlite-locked-extended", "server-no-pgcode",
        "server-08", "server-57P01", "server-53300", "server-invalidated", "pool-timeout",
        "no-such-table", "cant-open", "server-28P01", "other-exception",
    ],
)
def test_classification(env, monkeypatch, make, expected):
    monkeypatch.setattr(auth_mod, "verify_token", _raiser(make()))
    resp = _manifest(env, env.headers)
    assert resp.status_code == expected, resp.text
    if expected == 503:
        _assert_unavailable(resp)


def test_rest_and_mcp_share_one_classification_function(env, monkeypatch):
    """REST resolves the classifier through ``opencrab.auth`` at call time, so a
    replacement there changes REST. MCP binds the same function object."""
    assert http_mod.token_store_unavailable_reason is auth_mod.token_store_unavailable_reason

    monkeypatch.setattr(auth_mod, "verify_token", _raiser(RuntimeError("plain")))
    assert _manifest(env, env.headers).status_code == 500
    monkeypatch.setattr(auth_mod, "token_store_unavailable_reason", lambda exc: "store-busy")
    _assert_unavailable(_manifest(env, env.headers))


def test_unchanged_answers_with_a_healthy_store(env):
    assert _manifest(env, env.headers).status_code == 200

    junk = _manifest(env, {"Authorization": "Bearer not-a-token"})
    assert junk.status_code == 401
    assert junk.headers["www-authenticate"] == "Bearer"
    assert junk.json() == {"detail": "Invalid or missing bearer token."}

    missing = _manifest(env)
    assert missing.status_code == 401
    assert missing.json() == {"detail": "Invalid or missing bearer token."}


def test_requests_without_a_usable_credential_stay_401_while_the_store_is_locked(env):
    """These requests need no lookup, so a locked store cannot change the answer."""
    with _exclusive_lock(env.db_path):
        none = _manifest(env)
        basic = _manifest(env, {"Authorization": "Basic Zm9vOmJhcg=="})
        blank = _manifest(env, {"Authorization": "Bearer "})
    for resp in (none, basic, blank):
        assert resp.status_code == 401, resp.text
        assert resp.headers["www-authenticate"] == "Bearer"


def test_store_flag_unavailable_does_not_gate_the_lookup(env):
    """The app builds the REST store once at startup, and nothing refreshes its
    ``available`` flag. A store that was busy at startup but works now still
    authenticates."""
    env.sql._available = False
    assert _manifest(env, env.headers).status_code == 200


# ---------------------------------------------------------------------------
# 4 -- the 503 leaves a fixed, bounded trace
# ---------------------------------------------------------------------------


def test_repeated_503_logs_one_fixed_line_without_secrets(env, caplog):
    with caplog.at_level(logging.WARNING, logger="opencrab.api"), _exclusive_lock(env.db_path):
        for _ in range(3):
            _assert_unavailable(_manifest(env, env.headers))

    lines = [r for r in caplog.records if r.name == "opencrab.api" and r.levelno == logging.WARNING]
    assert len(lines) == 1
    text = lines[0].getMessage()
    assert "store-busy" in text
    assert env.secret not in text
    assert "SELECT" not in text
    assert "api_tokens" not in text


def test_log_limit_is_per_reason_and_expires(env, monkeypatch, caplog):
    from types import SimpleNamespace

    clock = [1000.0]
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def lines() -> list[str]:
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == "opencrab.api" and r.levelno == logging.WARNING
        ]

    with caplog.at_level(logging.WARNING, logger="opencrab.api"):
        monkeypatch.setattr(auth_mod, "verify_token", _raiser(_busy_error()))
        _manifest(env, env.headers)
        monkeypatch.setattr(auth_mod, "verify_token", _raiser(PoolTimeoutError("pool exhausted")))
        _manifest(env, env.headers)  # another reason: logged although the first was
        _manifest(env, env.headers)  # same reason inside the interval: not logged
        assert len(lines()) == 2

        clock[0] += api._UNAVAILABLE_LOG_INTERVAL + 0.5
        _manifest(env, env.headers)  # interval over: logged again
    assert len(lines()) == 3
    assert "store-unavailable" in lines()[-1]


def test_log_limit_reads_the_clock_and_logs_inside_its_lock(env, monkeypatch):
    """The table order must be the log order, so the clock read, the compare,
    the update and the log call share one lock."""
    from types import SimpleNamespace

    held_at_clock: list[bool] = []
    held_at_log: list[bool] = []

    def clock() -> float:
        held_at_clock.append(api._UNAVAILABLE_LOG_LOCK.locked())
        return 1000.0

    class _Probe(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            held_at_log.append(api._UNAVAILABLE_LOG_LOCK.locked())

    probe = _Probe(level=logging.WARNING)
    logger = logging.getLogger("opencrab.api")
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(auth_mod, "verify_token", _raiser(_busy_error()))
    logger.addHandler(probe)
    try:
        _assert_unavailable(_manifest(env, env.headers))
    finally:
        logger.removeHandler(probe)

    assert held_at_clock == [True]
    assert held_at_log == [True]
