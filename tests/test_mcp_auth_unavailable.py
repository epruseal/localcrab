"""#580 -- a token store that is busy or unreachable is not an invalid token.

Before the fix, ``_verify`` swallowed every ``verify_token`` error and the
caller turned the resulting ``None`` into a 401, so a valid token met a locked
``opencrab.db`` with the same answer as a junk token. A 401 sends an MCP client
into an OAuth flow this server does not have. These tests drive a real
``create_app()`` through ``httpx.ASGITransport`` inside one event loop. They pin
the 503 contract, the request deadline, the queue bound, and the worker's
recovery paths. See ``docs/issue-580-auth-unavailable.md`` for the table of
tests and the mutations each one must catch.

Discipline (same as tests/test_http_app_offload.py): nothing passes because a
sleep was long enough. Gated lookups signal ``started`` before the test sends
the next request, and every wait is bounded. The bound is a harness termination
condition, not a pass condition.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import gc
import logging
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from opencrab.auth import bootstrap_local_user
from opencrab.config import get_settings
from opencrab.mcp import http_app as http_mod
from opencrab.mcp.http_app import create_app
from opencrab.stores.sql_store import SQLStore

REPO_ROOT = Path(__file__).resolve().parents[1]

# Harness bound: it only stops a hung test. Nothing passes because of it.
_WAIT = 20.0
# How long a gated lookup holds before it gives up waiting for the test.
_HOLD = 15.0

_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class _Env:
    def __init__(self, tmp_path: Path, secret: str, user_id: str) -> None:
        self.tmp_path = tmp_path
        self.db_path = tmp_path / "opencrab.db"
        self.secret = secret
        self.user_id = user_id

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}"}


def _point_at(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    get_settings.cache_clear()


def _worker():
    return http_mod._POOLS.get("auth")


def _stop_workers() -> None:
    """Stop the auth worker (or, before the fix, the auth executor) of a test."""
    for pool in list(http_mod._POOLS.values()):
        stop = getattr(pool, "stop_for_tests", None)
        if stop is not None:
            stop()
        else:
            pool.shutdown(wait=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    sql = SQLStore(f"sqlite:///{tmp_path}/opencrab.db")
    user_id, secret = bootstrap_local_user(sql)
    sql.close()
    _point_at(tmp_path, monkeypatch)
    # The worker belongs to the process (see http_app). Give this test its own.
    monkeypatch.setattr(http_mod, "_POOLS", {}, raising=False)
    monkeypatch.setattr(http_mod, "_UNAVAILABLE_LOGGED", {}, raising=False)
    try:
        yield _Env(tmp_path, secret, user_id)
    finally:
        _stop_workers()
        get_settings.cache_clear()


def _run(coro_fn, *, allow_query_token: bool = False):
    """Run ``coro_fn(client)`` inside one event loop against a fresh app."""

    async def main():
        app = create_app(allow_query_token=allow_query_token)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await asyncio.wait_for(coro_fn(client), timeout=_WAIT * 2)

    return asyncio.run(main())


async def _until(pred, what: str, timeout: float = _WAIT) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def _post(client, headers, **kw):
    return await client.post("/mcp", json=_TOOLS_LIST, headers=headers, **kw)


def _queued() -> int:
    worker = _worker()
    return worker.queued if worker is not None and hasattr(worker, "queued") else -1


class _Gate:
    """A ``verify_token`` stand-in that can hold lookups, and records them."""

    def __init__(self, monkeypatch) -> None:
        self.real = http_mod.verify_token
        self.guard = threading.Lock()
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.threads: list[threading.Thread] = []
        self.hold = True
        self.started = threading.Event()
        self.release = threading.Event()
        self.raise_after_release: BaseException | None = None
        monkeypatch.setattr(http_mod, "verify_token", self)

    def __call__(self, store, presented):
        with self.guard:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.threads.append(threading.current_thread())
            hold = self.hold
        try:
            if hold:
                self.started.set()
                assert self.release.wait(timeout=_HOLD), "gated lookup was never released"
            with self.guard:
                failure, self.raise_after_release = self.raise_after_release, None
            if failure is not None:
                raise failure
            return self.real(store, presented)
        finally:
            with self.guard:
                self.active -= 1


def _busy_error() -> OperationalError:
    orig = sqlite3.OperationalError("database is locked")
    orig.sqlite_errorcode = 5  # SQLITE_BUSY
    return OperationalError("SELECT ...", {"token_hash": "h"}, orig)


def _raiser(exc: BaseException):
    def _verify(store, presented):  # noqa: ARG001
        raise exc

    return _verify


def _assert_unavailable(resp) -> None:
    assert resp.status_code == 503, resp.text
    assert resp.headers["retry-after"] == "5"
    assert resp.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in resp.headers


@contextlib.contextmanager
def _exclusive_lock(db_path: Path):
    """Hold the whole file the way a committing writer does."""
    conn = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    conn.execute("BEGIN EXCLUSIVE")
    try:
        yield
    finally:
        conn.execute("ROLLBACK")
        conn.close()


# ---------------------------------------------------------------------------
# 1 -- the acceptance criterion: a locked token file is a 503, not a 401
# ---------------------------------------------------------------------------


def test_locked_token_file_is_503_then_recovers_with_the_configured_busy_timeout(env):
    """No patching of the busy timeout: the lookup waits out ``SQLStore``'s own
    5-second wait, fails with ``database is locked``, and must answer 503."""

    async def scenario(client):
        with _exclusive_lock(env.db_path):
            locked = await _post(client, env.headers)
        healed = await _post(client, env.headers)
        return locked, healed

    locked, healed = _run(scenario)

    _assert_unavailable(locked)
    assert healed.status_code == 200, "the failed lookup was cached or the worker is stuck"


def test_locked_token_file_is_503_on_the_query_token_path(env, monkeypatch):
    import sqlalchemy

    real_create_engine = sqlalchemy.create_engine

    def short_timeout(*args, **kwargs):
        connect_args = dict(kwargs.get("connect_args") or {})
        connect_args["timeout"] = 0.2
        kwargs["connect_args"] = connect_args
        return real_create_engine(*args, **kwargs)

    monkeypatch.setattr(sqlalchemy, "create_engine", short_timeout)

    async def scenario(client):
        url = f"/mcp?token={env.secret}"
        with _exclusive_lock(env.db_path):
            locked = await client.post(url, json=_TOOLS_LIST)
        healed = await client.post(url, json=_TOOLS_LIST)
        return locked, healed

    locked, healed = _run(scenario, allow_query_token=True)

    _assert_unavailable(locked)
    assert healed.status_code == 200


# ---------------------------------------------------------------------------
# 2 -- every /mcp route answers the same way
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["post", "get", "delete"])
def test_all_mcp_routes_answer_503_for_a_busy_store(env, monkeypatch, method):
    monkeypatch.setattr(http_mod, "verify_token", _raiser(_busy_error()))

    async def scenario(client):
        if method == "post":
            return await client.post("/mcp", json=_TOOLS_LIST, headers=env.headers)
        return await getattr(client, method)("/mcp", headers=env.headers)

    _assert_unavailable(_run(scenario))


# ---------------------------------------------------------------------------
# 3 -- what is unavailable (503) and what stays a 401
# ---------------------------------------------------------------------------


class _PgError(Exception):
    def __init__(self, pgcode):
        super().__init__("driver error")
        self.pgcode = pgcode


def _server_error(pgcode, **kwargs) -> OperationalError:
    return OperationalError("SELECT ...", {}, _PgError(pgcode), **kwargs)


def _sqlite_error(message: str, code: int) -> OperationalError:
    orig = sqlite3.OperationalError(message)
    orig.sqlite_errorcode = code
    return OperationalError("SELECT ...", {}, orig)


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (_busy_error, 503),
        (lambda: _sqlite_error("database table is locked", 6), 503),
        (lambda: _sqlite_error("database table is locked", 262), 503),  # SQLITE_LOCKED_SHAREDCACHE
        (lambda: _server_error(None), 503),  # driver-side connection failure
        (lambda: _server_error("08006"), 503),
        (lambda: _server_error("57P01"), 503),
        (lambda: _server_error("53300"), 503),
        (lambda: _server_error("28P01", connection_invalidated=True), 503),
        (lambda: PoolTimeoutError("pool exhausted"), 503),
        # Not "temporarily unavailable": these stay what they were.
        (lambda: _sqlite_error("no such table: users", 1), 401),
        (lambda: _sqlite_error("no such column: t.revoked_at", 1), 401),
        (lambda: _sqlite_error("unable to open database file", 14), 401),
        (lambda: _server_error("28P01"), 401),
        (lambda: _server_error("53100"), 401),
        (lambda: RuntimeError("anything else"), 401),
    ],
    ids=[
        "sqlite-busy", "sqlite-locked", "sqlite-locked-extended", "server-no-pgcode",
        "server-08", "server-57P01", "server-53300", "server-invalidated", "pool-timeout",
        "no-such-table", "no-such-column", "cant-open", "server-28P01", "server-53100",
        "other-exception",
    ],
)
def test_classification(env, monkeypatch, make, expected):
    monkeypatch.setattr(http_mod, "verify_token", _raiser(make()))

    async def scenario(client):
        return await _post(client, env.headers)

    resp = _run(scenario)
    assert resp.status_code == expected, resp.text
    if expected == 503:
        _assert_unavailable(resp)
    else:
        assert resp.headers["www-authenticate"] == "Bearer"


def test_unreachable_store_is_503_and_recovers(env, monkeypatch):
    """A connect failure (``available == False``) is a 503, and is not cached."""
    import opencrab.stores.sql_store as store_mod

    real_init = store_mod.SQLStore.__init__
    state = {"fail": True}

    def flaky(self, url, create_tables=True):  # noqa: FBT002
        real_init(self, url, create_tables=create_tables)
        if state["fail"]:
            self._available = False
            state["fail"] = False

    monkeypatch.setattr(store_mod.SQLStore, "__init__", flaky)

    async def scenario(client):
        return await _post(client, env.headers), await _post(client, env.headers)

    down, up = _run(scenario)
    _assert_unavailable(down)
    assert up.status_code == 200


def test_invalid_token_and_missing_file_and_empty_database_stay_401(env, tmp_path, monkeypatch):
    async def junk(client):
        return await _post(client, {"Authorization": "Bearer lc_junk"})

    resp = _run(junk)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    _stop_workers()

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    (empty_dir / "opencrab.db").touch()  # a file with no tables
    _point_at(empty_dir, monkeypatch)
    monkeypatch.setattr(http_mod, "_POOLS", {}, raising=False)
    try:
        assert _run(junk).status_code == 401, "an uninitialised database holds no valid token"
    finally:
        _stop_workers()

    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    _point_at(missing_dir, monkeypatch)
    monkeypatch.setattr(http_mod, "_POOLS", {}, raising=False)
    try:
        assert _run(junk).status_code == 401, "no database file means nothing to look up"
        assert not (missing_dir / "opencrab.db").exists()
    finally:
        _stop_workers()


# ---------------------------------------------------------------------------
# 4 -- requests that need no lookup never enter the queue
# ---------------------------------------------------------------------------


def test_requests_without_a_candidate_token_stay_401_when_the_queue_is_full(env, monkeypatch):
    monkeypatch.setattr(http_mod, "_AUTH_QUEUE_LIMIT", 1, raising=False)
    gate = _Gate(monkeypatch)

    async def scenario(client):
        running = asyncio.create_task(_post(client, env.headers))
        await _until(gate.started.is_set, "first lookup start")
        queued = asyncio.create_task(_post(client, env.headers))
        await _until(lambda: _queued() == 1, "second lookup queued")
        cases = {
            "no header": {},
            "non-bearer": {"Authorization": "Basic abc"},
            "empty header": {"Authorization": ""},
            "blank bearer": {"Authorization": "Bearer    "},
        }
        answers = {}
        for name, headers in cases.items():
            answers[name] = await asyncio.wait_for(_post(client, headers), 5)
        answers["blank query"] = await asyncio.wait_for(
            client.post("/mcp?token=%20%20", json=_TOOLS_LIST), 5
        )
        calls_when_full = gate.calls
        full = await asyncio.wait_for(_post(client, env.headers), 5)
        gate.release.set()
        return answers, calls_when_full, full, await running, await queued

    answers, calls_when_full, full, first, second = _run(scenario, allow_query_token=True)

    for name, resp in answers.items():
        assert resp.status_code == 401, name
    assert calls_when_full == 1, "a request without a candidate token reached the worker"
    _assert_unavailable(full)  # contrast: a lookup is needed, so a full queue is a 503
    assert first.status_code == 200 and second.status_code == 200


# ---------------------------------------------------------------------------
# 5 -- the queue bound
# ---------------------------------------------------------------------------


def test_full_queue_answers_503_at_once(env, monkeypatch):
    monkeypatch.setattr(http_mod, "_AUTH_QUEUE_LIMIT", 2, raising=False)
    gate = _Gate(monkeypatch)

    async def scenario(client):
        accepted = [asyncio.create_task(_post(client, env.headers))]
        await _until(gate.started.is_set, "first lookup start")
        for expected in (1, 2):
            accepted.append(asyncio.create_task(_post(client, env.headers)))
            await _until(lambda expected=expected: _queued() == expected, f"queue size {expected}")
        # The harness bound is 5 s. Waiting for the held lookups would exceed it.
        overflow = await asyncio.wait_for(_post(client, env.headers), 5)
        queued_after = _queued()
        gate.release.set()
        return overflow, queued_after, [await t for t in accepted]

    overflow, queued_after, answers = _run(scenario)

    _assert_unavailable(overflow)
    assert queued_after == 2, "the overflow request was queued"
    assert [r.status_code for r in answers] == [200, 200, 200]
    assert gate.calls == 3


# ---------------------------------------------------------------------------
# 6, 7 -- the request deadline, and abandoned work stays bounded
# ---------------------------------------------------------------------------


def test_deadline_gives_503_and_abandoned_lookups_never_run(env, monkeypatch):
    monkeypatch.setattr(http_mod, "_AUTH_DEADLINE_SECONDS", 0.3, raising=False)
    gate = _Gate(monkeypatch)

    async def scenario(client):
        running = asyncio.create_task(_post(client, env.headers))
        await _until(gate.started.is_set, "first lookup start")
        waiting = asyncio.create_task(_post(client, env.headers))
        await _until(lambda: _queued() == 1, "second lookup queued")
        timed_out = await asyncio.wait_for(asyncio.gather(running, waiting), 10)
        gate.release.set()
        # The worker now pops the abandoned item. A later request is a barrier:
        # it is answered only after that item was handled.
        monkeypatch.setattr(http_mod, "_AUTH_DEADLINE_SECONDS", 30.0)
        gate.hold = False
        barrier = await _post(client, env.headers)
        return timed_out, barrier

    timed_out, barrier = _run(scenario)

    for resp in timed_out:
        _assert_unavailable(resp)
    assert barrier.status_code == 200
    assert gate.calls == 2, "the abandoned lookup ran after its request gave up"
    assert _queued() == 0


def test_repeated_abandonment_cannot_grow_the_queue(env, monkeypatch):
    monkeypatch.setattr(http_mod, "_AUTH_QUEUE_LIMIT", 3, raising=False)
    monkeypatch.setattr(http_mod, "_AUTH_DEADLINE_SECONDS", 0.2, raising=False)
    gate = _Gate(monkeypatch)
    sizes: list[int] = []

    async def scenario(client):
        running = asyncio.create_task(_post(client, env.headers))
        await _until(gate.started.is_set, "first lookup start")
        answers = []
        for _ in range(8):
            answers.append(await asyncio.wait_for(_post(client, env.headers), 10))
            sizes.append(_queued())
        gate.release.set()
        gate.hold = False
        await running
        return answers

    answers = _run(scenario)

    assert all(r.status_code == 503 for r in answers)
    assert max(sizes) == 3, f"queue sizes {sizes}"
    assert gate.calls == 1
    assert {"deadline", "queue-full"} <= set(http_mod._UNAVAILABLE_LOGGED)


# ---------------------------------------------------------------------------
# 8 -- worker recovery
# ---------------------------------------------------------------------------


def test_thread_start_failure_leaves_nothing_behind_and_heals(env, monkeypatch):
    real_start = threading.Thread.start
    state = {"fail": True}

    def start(self):
        if self.name == "mcp-auth" and state["fail"]:
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(http_mod, "_AUTH_QUEUE_LIMIT", 2, raising=False)

    async def scenario(client):
        failures = 0
        for _ in range(6):  # more attempts than the queue holds
            with pytest.raises(RuntimeError, match="can't start new thread"):
                await _post(client, env.headers)
            failures += 1
        stranded = _queued()
        state["fail"] = False
        return failures, stranded, await _post(client, env.headers)

    failures, stranded, healed = _run(scenario)
    assert failures == 6
    assert stranded == 0, "a failed start left an item in the queue"
    assert healed.status_code == 200


def test_cancelled_request_never_runs_its_lookup(env, monkeypatch):
    gate = _Gate(monkeypatch)

    async def scenario(client):
        running = asyncio.create_task(_post(client, env.headers))
        await _until(gate.started.is_set, "first lookup start")
        waiting = asyncio.create_task(_post(client, env.headers))
        await _until(lambda: _queued() == 1, "second lookup queued")
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting
        gate.release.set()
        gate.hold = False
        barrier = await _post(client, env.headers)
        return await running, barrier

    first, barrier = _run(scenario)

    assert first.status_code == 200 and barrier.status_code == 200
    assert gate.calls == 2, "a cancelled request's lookup ran"
    assert _queued() == 0


class _Fatal(BaseException):
    """What `_lookup` does not catch: it only handles `Exception`."""


@pytest.mark.parametrize("late", ["result", "http-503", "base-exception"])
def test_late_outcome_after_the_deadline_raises_nothing_in_the_loop(env, monkeypatch, late):
    """A request gives up and cancels its future. The lookup ends late. It has one
    of three outcomes. The first is a result. The second is an `HTTPException`
    that leaves `_lookup` (a busy store). The third is a `BaseException` that
    `_lookup` does not catch. The worker delivers the outcome to the cancelled
    future. The loop must report nothing."""
    monkeypatch.setattr(http_mod, "_AUTH_DEADLINE_SECONDS", 0.2, raising=False)
    gate = _Gate(monkeypatch)
    if late == "http-503":
        gate.raise_after_release = _busy_error()
    elif late == "base-exception":
        gate.raise_after_release = _Fatal()
    reported: list[dict] = []
    futures: list = []
    finished = threading.Event()
    real_run = http_mod._AuthJob.run

    def observed_run(self):
        first = not futures
        if first:
            futures.append(self._future)
        try:
            return real_run(self)
        finally:
            if first:
                finished.set()

    monkeypatch.setattr(http_mod._AuthJob, "run", observed_run, raising=False)

    async def scenario(client):
        asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: reported.append(ctx))
        running = asyncio.create_task(_post(client, env.headers))
        await _until(gate.started.is_set, "first lookup start")
        timed_out = await asyncio.wait_for(running, 10)
        # The lookup still holds. The request has answered and cancelled its future.
        assert futures[0].cancelled(), "the request kept its future after it gave up"
        gate.release.set()
        gate.hold = False
        await _until(finished.is_set, "the late job to end")
        monkeypatch.setattr(http_mod, "_AUTH_DEADLINE_SECONDS", 30.0)
        # The barrier is answered after the worker delivered the late outcome.
        barrier = await _post(client, env.headers)
        futures.clear()
        for _ in range(5):
            await asyncio.sleep(0.02)
        gc.collect()
        await asyncio.sleep(0.02)
        return timed_out, barrier

    timed_out, barrier = _run(scenario)

    _assert_unavailable(timed_out)
    assert barrier.status_code == 200
    assert reported == [], f"the loop reported {reported}"


def test_worker_survives_a_loop_that_closed_before_its_lookup_ended(env, monkeypatch):
    monkeypatch.setattr(http_mod, "_AUTH_DEADLINE_SECONDS", 0.2, raising=False)
    gate = _Gate(monkeypatch)

    async def first(client):
        return await asyncio.wait_for(_post(client, env.headers), 10)

    async def held(client):
        task = asyncio.create_task(first(client))
        await _until(gate.started.is_set, "lookup start")
        return await task

    timed_out = _run(held)  # the loop is closed when this returns; the lookup still holds
    _assert_unavailable(timed_out)
    worker = _worker()
    gate.release.set()
    gate.hold = False
    monkeypatch.setattr(http_mod, "_AUTH_DEADLINE_SECONDS", 30.0)

    async def after(client):
        return await _post(client, env.headers)

    resp = _run(after)
    assert resp.status_code == 200
    assert worker is _worker() and worker._thread.is_alive()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_dead_worker_thread_is_replaced_and_keeps_the_queue(env, monkeypatch):
    """The loop itself failing is the only way the thread dies. The queue
    outlives it, and the next submission starts a new thread that drains it."""
    monkeypatch.setattr(http_mod, "_AUTH_DEADLINE_SECONDS", 3.0, raising=False)
    real_run = http_mod._AuthJob.run if hasattr(http_mod, "_AuthJob") else None
    release = threading.Event()
    in_run = threading.Event()
    state = {"kill": True}

    class _LoopFailure(BaseException):
        pass

    def run(self):
        if state["kill"]:
            state["kill"] = False
            in_run.set()
            assert release.wait(timeout=_HOLD)
            raise _LoopFailure
        return real_run(self)

    monkeypatch.setattr(http_mod._AuthJob, "run", run, raising=False)

    async def scenario(client):
        doomed = asyncio.create_task(_post(client, env.headers))
        await _until(in_run.is_set, "worker inside the failing job")
        survivor = asyncio.create_task(_post(client, env.headers))
        await _until(lambda: _queued() == 1, "survivor queued")
        dead = _worker()._thread
        release.set()
        await _until(lambda: not dead.is_alive(), "worker thread death")
        assert _queued() == 1, "the queue lost its item with the thread"
        fresh = await _post(client, env.headers)  # starts a new thread
        return await doomed, await survivor, fresh, dead

    doomed, survivor, fresh, dead = _run(scenario)

    _assert_unavailable(doomed)  # lost in flight; its request ended at the deadline
    assert survivor.status_code == 200, "the item queued before the death was not served"
    assert fresh.status_code == 200
    assert _worker()._thread is not dead and _worker()._thread.is_alive()


def test_lookup_raising_a_base_exception_fails_only_that_request(env, monkeypatch):
    class _Fatal(BaseException):
        pass

    gate = _Gate(monkeypatch)
    gate.hold = False
    gate.raise_after_release = _Fatal()

    async def scenario(client):
        outcome = None
        try:
            await _post(client, env.headers)
        except BaseException as exc:  # noqa: BLE001 - what the client sees is the point
            outcome = exc
        return outcome, await _post(client, env.headers)

    outcome, next_resp = _run(scenario)

    assert outcome is not None, "the failure was swallowed into a success"
    assert next_resp.status_code == 200
    assert _worker()._thread.is_alive()


# ---------------------------------------------------------------------------
# 9 -- log
# ---------------------------------------------------------------------------


def test_repeated_503_logs_once_without_secrets(env, monkeypatch, caplog):
    monkeypatch.setattr(http_mod, "verify_token", _raiser(_busy_error()))

    async def scenario(client):
        return [await _post(client, env.headers) for _ in range(3)]

    with caplog.at_level(logging.WARNING, logger=http_mod.logger.name):
        answers = _run(scenario)

    assert [r.status_code for r in answers] == [503, 503, 503]
    records = [r for r in caplog.records if r.name == http_mod.logger.name]
    assert len(records) == 1
    text = records[0].getMessage()
    assert "store-busy" in text
    assert env.secret not in text and "SELECT" not in text and "token_hash" not in text


# ---------------------------------------------------------------------------
# 11 -- the test-only stop is never called by product code
# ---------------------------------------------------------------------------


def test_stop_for_tests_ends_the_worker_thread(env):
    async def scenario(client):
        return await _post(client, env.headers)

    assert _run(scenario).status_code == 200
    worker = _worker()
    thread = worker._thread
    assert thread.is_alive() and thread.name == "mcp-auth" and thread.daemon
    worker.stop_for_tests()
    assert not thread.is_alive()


def test_no_product_code_calls_stop_for_tests():
    calls = []
    for root in ("opencrab", "apps"):
        for path in (REPO_ROOT / root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                    if name == "stop_for_tests":
                        calls.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert calls == []
    # And the name exists, so the scan above is looking for something real.
    assert hasattr(http_mod, "_AuthWorker") and hasattr(http_mod._AuthWorker, "stop_for_tests")
    doc = http_mod._AuthWorker.stop_for_tests.__doc__ or ""
    assert "test" in doc.lower()
