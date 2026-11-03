"""#577 -- slow tool execution must not block the HTTP MCP event loop.

``mcp_post`` used to run ``server.handle_request`` on the event loop thread, so
one slow tool (a write tool embedding on the CPU fallback) stopped ``/healthz``
and every other request of the process. These tests drive a real ``create_app()``
through ``httpx.ASGITransport`` inside ONE event loop, so a blocked loop shows up
as stalled requests, not as a thread race.

Discipline (same as tests/test_mcp_context_init_race.py): nothing passes because
a sleep was long enough. Probe tools signal ``started`` before the test sends any
other request, every wait is bounded, and the bound is a harness termination
condition, not a pass condition. The cold-context cases (T9) are the exception
to read carefully: some of their bounded waits can expire on the normal path,
and what they prove is asserted separately. Those cases use the real
``_get_context`` and ``_context_init_lock``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from opencrab.auth import Principal, bootstrap_local_user, current_principal
from opencrab.config import get_settings
from opencrab.mcp import http_app as http_mod
from opencrab.mcp import tools as tools_mod
from opencrab.mcp.http_app import create_app
from opencrab.mcp.server import MCPServer
from opencrab.mcp.tools._registry import _REGISTRY, AccessTier, tool
from opencrab.stores.sql_store import SQLStore

# Upper bound that only stops a harness from hanging when an expectation is
# already violated. Nothing passes because of this number.
_WAIT = 30.0
# How long a probe holds before it gives up waiting for the test to release it.
# On the unfixed code the loop is blocked for this long, then the order
# assertions fail, so the red run is bounded and never hangs.
_HOLD = 5.0


# ---------------------------------------------------------------------------
# Probe state and registration
# ---------------------------------------------------------------------------


class _State:
    """What the probe tools observed, shared between worker threads and the test."""

    def __init__(self) -> None:
        self.guard = threading.Lock()
        self._events: dict[tuple[str, str], threading.Event] = {}
        self.released: dict[str, bool] = {}
        self.order: list[tuple[str, str]] = []
        self.threads: dict[str, str] = {}
        self.principals: dict[str, str] = {}
        self.active_writes = 0
        self.max_active_writes = 0
        self.active_at_read: dict[str, int] = {}

    def event(self, kind: str, tag: str) -> threading.Event:
        with self.guard:
            return self._events.setdefault((kind, tag), threading.Event())

    def enter_write(self, tag: str) -> None:
        with self.guard:
            self.active_writes += 1
            self.max_active_writes = max(self.max_active_writes, self.active_writes)
            self.order.append(("start", tag))
            self.threads[tag] = threading.current_thread().name
            self.principals[tag] = current_principal().user_id

    def leave_write(self, tag: str) -> None:
        with self.guard:
            self.active_writes -= 1
            self.order.append(("end", tag))


_SCHEMA = {
    "description": "probe tool for issue 577 tests",
    "inputSchema": {
        "type": "object",
        "properties": {"tag": {"type": "string"}, "hold": {"type": "boolean"}},
    },
}


@contextlib.contextmanager
def _registered(state: _State):
    """Register the probe tools, and remove them again whatever happens."""

    def probe_w(tag: str = "w", hold: bool = False) -> dict:
        state.enter_write(tag)
        state.event("started", tag).set()
        if hold:
            state.released[tag] = state.event("release", tag).wait(timeout=_HOLD)
        state.leave_write(tag)
        state.event("finished", tag).set()
        return {"tag": tag}

    def probe_r(tag: str = "r", hold: bool = False) -> dict:  # noqa: ARG001
        with state.guard:
            state.threads[tag] = threading.current_thread().name
            state.principals[tag] = current_principal().user_id
            state.active_at_read[tag] = state.active_writes
            state.order.append(("read", tag))
        state.event("started", tag).set()
        return {"tag": tag}

    names = ["probe_w", "probe_r"]
    tool("probe_w", _SCHEMA, access=AccessTier.WRITE, order=9001, writes=True)(probe_w)
    tool("probe_r", _SCHEMA, access=AccessTier.READ, order=9002, writes=False)(probe_r)
    try:
        yield
    finally:
        for name in names:
            _REGISTRY.pop(name, None)


class _Env:
    def __init__(self, state, secret, user_id, created):
        self.state = state
        self.secret = secret
        self.user_id = user_id
        self.created = created  # recording executors, in creation order
        self.auth_baseline: frozenset = frozenset()  # mcp-auth threads that predate the test

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}"}

    def widths(self) -> dict[str, int]:
        return {e.prefix: e.width for e in self.created}

    def submits(self, prefix: str) -> int:
        return sum(e.submits for e in self.created if e.prefix == prefix)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A bootstrapped token, a non-empty ``_context`` (so routing is by tool
    kind, not by the cold-context rule), recording executors and probe tools."""
    sql = SQLStore(f"sqlite:///{tmp_path}/opencrab.db")
    user_id, secret = bootstrap_local_user(sql)
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    get_settings.cache_clear()
    monkeypatch.setattr(tools_mod, "_context", {"stub": object()})

    created: list = []

    class Recording(ThreadPoolExecutor):
        def __init__(self, max_workers=None, thread_name_prefix="", **kwargs):
            super().__init__(
                max_workers=max_workers, thread_name_prefix=thread_name_prefix, **kwargs
            )
            self.prefix = thread_name_prefix
            self.width = max_workers
            self.submits = 0
            created.append(self)

        def submit(self, fn, /, *args, **kwargs):
            self.submits += 1
            return super().submit(fn, *args, **kwargs)

    # raising=False: on the unfixed code the module has no such name; the
    # assertions on ``created`` then fail for the right reason.
    monkeypatch.setattr(http_mod, "ThreadPoolExecutor", Recording, raising=False)
    # The pools belong to the process (see http_app); give this test its own
    # and shut down the pools this test created afterwards.
    monkeypatch.setattr(http_mod, "_POOLS", {}, raising=False)

    state = _State()
    try:
        with _registered(state):
            test_env = _Env(state, secret, user_id, created)
            test_env.auth_baseline = frozenset(
                t for t in threading.enumerate() if t.name == "mcp-auth"
            )
            yield test_env
    finally:
        _stop_auth_workers()
        for executor in created:
            executor.shutdown(wait=True)
        get_settings.cache_clear()


def _run(coro_fn, *, remote_errors: bool = True):
    """Run ``coro_fn(client)`` inside one event loop against a fresh app."""

    async def main():
        app = create_app()
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=remote_errors)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await asyncio.wait_for(coro_fn(client), timeout=_WAIT * 2)

    return asyncio.run(main())


async def _until(pred, what: str, timeout: float = _WAIT) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


def _call(name: str, args: dict | None = None, rid: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    }


async def _post(client, env: _Env, body):
    return await client.post("/mcp", json=body, headers=env.headers)


def _text(resp) -> dict:
    return json.loads(resp.json()["result"]["content"][0]["text"])


def _auth_threads(env: _Env) -> list[threading.Thread]:
    """Live ``mcp-auth`` threads that this test created. The fixture ignores
    threads that predate it: they belong to the process-wide worker of other
    test modules."""
    return [
        t
        for t in threading.enumerate()
        if t.name == "mcp-auth" and t.is_alive() and t not in env.auth_baseline
    ]


def _stop_auth_workers() -> None:
    for pool in list(http_mod._POOLS.values()):
        stop = getattr(pool, "stop_for_tests", None)
        if stop is not None:
            stop()


# ---------------------------------------------------------------------------
# T1 -- the loop stays free while a write runs
# ---------------------------------------------------------------------------


def test_loop_answers_while_a_write_tool_runs(env):
    state = env.state

    async def scenario(client):
        write = asyncio.create_task(_post(client, env, _call("probe_w", {"tag": "a", "hold": True})))
        # Only now send anything else: the probe is really running.
        await _until(state.event("started", "a").is_set, "write probe start")

        health = await client.get("/healthz")
        init = await _post(
            client, env,
            {"jsonrpc": "2.0", "id": 2, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {}}},
        )
        listing = await _post(client, env, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        read = await _post(client, env, _call("probe_r", {"tag": "r1"}, rid=4))

        # The write has not finished while all of these were answered.
        write_done_when_answered = state.event("finished", "a").is_set()

        state.event("release", "a").set()
        write_resp = await write
        return health, init, listing, read, write_done_when_answered, write_resp

    health, init, listing, read, done_early, write_resp = _run(scenario)

    assert not done_early, "healthz and reads were answered only after the write finished"
    assert state.released["a"] is True, "the write probe timed out instead of being released"
    assert health.status_code == 200 and health.text == "ok"
    assert init.status_code == 200 and "serverInfo" in init.json()["result"]
    assert listing.status_code == 200 and "tools" in listing.json()["result"]
    assert read.status_code == 200 and _text(read) == {"tag": "r1"}
    assert write_resp.status_code == 200 and _text(write_resp) == {"tag": "a"}
    # The read ran while the write was in flight.
    assert state.active_at_read["r1"] == 1


# ---------------------------------------------------------------------------
# T2 -- one write thread, pool widths
# ---------------------------------------------------------------------------


def test_write_calls_share_one_thread_and_do_not_overlap(env):
    state = env.state

    async def scenario(client):
        a = asyncio.create_task(_post(client, env, _call("probe_w", {"tag": "a", "hold": True})))
        await _until(state.event("started", "a").is_set, "write a start")
        b = asyncio.create_task(_post(client, env, _call("probe_w", {"tag": "b"}, rid=2)))
        # b is queued behind a on the single write thread.
        await _until(lambda: env.submits("mcp-write") == 2, "write b submission")
        read = await _post(client, env, _call("probe_r", {"tag": "r1"}, rid=3))
        state.event("release", "a").set()
        await asyncio.gather(a, b)
        return read

    read = _run(scenario)

    assert read.status_code == 200
    assert state.released["a"] is True, "the held write timed out instead of being released"
    assert state.max_active_writes == 1
    assert state.order.index(("end", "a")) < state.order.index(("start", "b"))
    assert state.threads["a"] == state.threads["b"]
    assert state.threads["a"].startswith("mcp-write")
    assert state.threads["r1"].startswith("mcp-read")
    assert env.widths() == {"mcp-write": 1, "mcp-read": 4}
    # The auth worker is not an executor any more (#580). Its width is one live
    # thread, counted from the threads themselves.
    assert len(_auth_threads(env)) == 1


# ---------------------------------------------------------------------------
# T3 -- threads and principal
# ---------------------------------------------------------------------------


def test_tools_and_token_check_run_off_the_loop_with_the_principal(env, monkeypatch):
    state = env.state
    verify_threads: list[str] = []
    verify_thread_objects: list[threading.Thread] = []
    active = {"now": 0, "max": 0}
    real_verify = http_mod.verify_token

    def spy(store, presented):
        verify_threads.append(threading.current_thread().name)
        verify_thread_objects.append(threading.current_thread())
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        try:
            return real_verify(store, presented)
        finally:
            active["now"] -= 1

    monkeypatch.setattr(http_mod, "verify_token", spy)
    loop_thread: list[str] = []

    async def scenario(client):
        loop_thread.append(threading.current_thread().name)
        await asyncio.gather(
            *[_post(client, env, _call("probe_r", {"tag": f"r{i}"}, rid=i)) for i in range(4)],
            _post(client, env, _call("probe_w", {"tag": "w1"}, rid=9)),
        )

    _run(scenario)

    assert loop_thread[0] not in verify_threads
    assert verify_threads and all(name.startswith("mcp-auth") for name in verify_threads)
    # The GET and DELETE routes check the token on the same thread (they answer
    # 405 after a valid token; the check itself is what must stay off the loop).
    before = len(verify_threads)
    _run(_get_and_delete_scenario(env))
    assert len(verify_threads) == before + 2
    assert all(name.startswith("mcp-auth") for name in verify_threads[before:])
    for tag in ("r0", "r1", "r2", "r3", "w1"):
        assert state.threads[tag] != loop_thread[0]
        assert state.principals[tag] == env.user_id
    # Auth concurrency is one: every lookup ran on the same thread object, one
    # at a time, and exactly one `mcp-auth` thread is alive.
    assert len(set(verify_thread_objects)) == 1
    assert active["max"] == 1
    assert len(_auth_threads(env)) == 1


def test_auth_lookups_run_one_at_a_time_on_one_thread(env, monkeypatch):
    """Auth width one, asserted by behaviour. Since #580 the auth worker is no
    executor, so a recording executor cannot show its width. While the first
    lookup holds, the second request's lookup waits in the queue and does not
    start. Afterwards both lookups ran on one thread object, and exactly one
    ``mcp-auth`` thread is alive. The wait below checks an absence. It can miss
    a second thread but never fails the single-thread code."""
    real_verify = http_mod.verify_token
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    guard = threading.Lock()
    seen: list[threading.Thread] = []

    def spy(store, presented):
        with guard:
            seen.append(threading.current_thread())
            first = len(seen) == 1
        if first:
            started.set()
            assert release.wait(timeout=_HOLD)
        else:
            second_started.set()
        return real_verify(store, presented)

    monkeypatch.setattr(http_mod, "verify_token", spy)

    async def scenario(client):
        a = asyncio.create_task(_post(client, env, _call("probe_r", {"tag": "a"}, rid=1)))
        await _until(started.is_set, "first lookup start")
        b = asyncio.create_task(_post(client, env, _call("probe_r", {"tag": "b"}, rid=2)))
        await asyncio.sleep(0.3)
        second_before_release = second_started.is_set()
        release.set()
        return second_before_release, await a, await b

    second_before_release, a, b = _run(scenario)

    assert not second_before_release, "a second lookup ran while the first was held"
    assert a.status_code == 200 and b.status_code == 200
    assert len(seen) == 2 and len(set(seen)) == 1
    assert len(_auth_threads(env)) == 1


def _get_and_delete_scenario(env):
    async def scenario(client):
        got = await client.get("/mcp", headers=env.headers)
        deleted = await client.delete("/mcp", headers=env.headers)
        assert got.status_code == 405 and deleted.status_code == 405

    return scenario


# ---------------------------------------------------------------------------
# T4 -- cancellation
# ---------------------------------------------------------------------------


def test_cancelling_a_running_write_lets_it_finish_before_the_next(env):
    state = env.state

    async def scenario(client):
        a = asyncio.create_task(_post(client, env, _call("probe_w", {"tag": "a", "hold": True})))
        await _until(state.event("started", "a").is_set, "write a start")
        a.cancel()
        await asyncio.gather(a, return_exceptions=True)
        b = asyncio.create_task(_post(client, env, _call("probe_w", {"tag": "b"}, rid=2)))
        await _until(lambda: env.submits("mcp-write") == 2, "write b submission")
        state.event("release", "a").set()
        return await b

    resp = _run(scenario)

    assert resp.status_code == 200
    assert state.released["a"] is True
    assert state.order == [("start", "a"), ("end", "a"), ("start", "b"), ("end", "b")]


def test_cancelling_a_queued_write_before_it_starts_means_it_never_runs(env):
    state = env.state

    async def scenario(client):
        a = asyncio.create_task(_post(client, env, _call("probe_w", {"tag": "a", "hold": True})))
        await _until(state.event("started", "a").is_set, "write a start")
        b = asyncio.create_task(_post(client, env, _call("probe_w", {"tag": "b"}, rid=2)))
        await _until(lambda: env.submits("mcp-write") == 2, "write b submission")
        b.cancel()
        # The pending future is cancelled on the next loop iteration, so wait
        # for the cancelled task to be done before releasing the first write.
        await asyncio.gather(b, return_exceptions=True)
        state.event("release", "a").set()
        await a
        # A third write passes the single write thread after b's queue slot.
        c = await _post(client, env, _call("probe_w", {"tag": "c"}, rid=3))
        return c

    resp = _run(scenario)

    assert resp.status_code == 200
    assert state.released["a"] is True, "the held write timed out instead of being released"
    assert ("start", "b") not in state.order
    assert state.order == [("start", "a"), ("end", "a"), ("start", "c"), ("end", "c")]


# ---------------------------------------------------------------------------
# T5 -- legacy batch
# ---------------------------------------------------------------------------


def test_batch_with_a_write_item_runs_in_order_on_one_write_thread(env):
    state = env.state

    async def scenario(client):
        batch = [
            _call("probe_w", {"tag": "b1"}, rid=1),
            _call("probe_r", {"tag": "br"}, rid=2),
            _call("probe_w", {"tag": "b2"}, rid=3),
        ]
        return await _post(client, env, batch)

    resp = _run(scenario)

    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()] == [1, 2, 3]
    assert state.order == [("start", "b1"), ("end", "b1"), ("read", "br"), ("start", "b2"), ("end", "b2")]
    assert {state.threads[t] for t in ("b1", "br", "b2")} == {state.threads["b1"]}
    assert state.threads["b1"].startswith("mcp-write")


def test_batch_of_non_tool_methods_stays_on_the_loop(env, monkeypatch):
    seen: list[tuple[object, str]] = []
    real = MCPServer.handle_request

    def spy(self, request):
        seen.append((request.get("method") if isinstance(request, dict) else None,
                     threading.current_thread().name))
        return real(self, request)

    monkeypatch.setattr(MCPServer, "handle_request", spy)
    loop_thread: list[str] = []

    async def scenario(client):
        loop_thread.append(threading.current_thread().name)
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        return await _post(client, env, batch)

    resp = _run(scenario)

    assert resp.status_code == 200
    assert seen and all(name == loop_thread[0] for _method, name in seen)


# ---------------------------------------------------------------------------
# T7 -- classification and malformed shapes
# ---------------------------------------------------------------------------


def test_tool_pool_kind_is_total_and_tier_aware():
    from opencrab.mcp.tools import tool_pool_kind

    local = Principal(user_id="local", is_local=True, disabled=False)
    remote = Principal(user_id="remote", is_local=False, disabled=False)

    assert tool_pool_kind("ontology_add_node", local) == "write"
    assert tool_pool_kind("ontology_list_nodes", local) == "read"
    assert tool_pool_kind("no_such_tool", local) is None
    # An ADMIN-tier tool is hidden from a remote principal, as in dispatch_tool.
    assert _REGISTRY["schema_pack_install"].access is AccessTier.ADMIN
    assert tool_pool_kind("schema_pack_install", local) == "write"
    assert tool_pool_kind("schema_pack_install", remote) is None
    for bad in (None, 7, 1.5, ["ontology_add_node"], {"a": 1}, b"x"):
        assert tool_pool_kind(bad, local) is None


@pytest.mark.parametrize(
    "params",
    ["not-a-dict", {"name": ["ontology_add_node"], "arguments": {}}, {"name": 7}, {}],
)
def test_malformed_tool_calls_get_an_error_envelope_as_before(env, params):
    async def scenario(client):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        return await _post(client, env, body)

    resp = _run(scenario)

    assert resp.status_code == 200
    assert "error" in resp.json()


def test_a_tools_call_notification_still_runs_the_tool(env):
    state = env.state

    async def scenario(client):
        body = {"jsonrpc": "2.0", "method": "tools/call",
                "params": {"name": "probe_w", "arguments": {"tag": "n"}}}
        return await _post(client, env, body)

    resp = _run(scenario)

    assert resp.status_code == 202
    assert state.event("finished", "n").is_set()
    assert state.threads["n"].startswith("mcp-write")


# ---------------------------------------------------------------------------
# Scope tests -- non-tool methods and unknown tools with a COLD context
# ---------------------------------------------------------------------------


@pytest.fixture
def cold(env, monkeypatch):
    """Empty ``_context`` and a counting ``_get_context`` that never builds."""
    monkeypatch.setattr(tools_mod, "_context", {})
    calls: list[str] = []

    def counting():
        calls.append(threading.current_thread().name)
        return {}

    monkeypatch.setattr(tools_mod, "_get_context", counting)
    return calls


def test_non_tool_methods_do_not_touch_the_context_and_stay_on_the_loop(env, cold, monkeypatch):
    seen: list[tuple[object, str]] = []
    real = MCPServer.handle_request

    def spy(self, request):
        seen.append((request.get("method"), threading.current_thread().name))
        return real(self, request)

    monkeypatch.setattr(MCPServer, "handle_request", spy)
    loop_thread: list[str] = []

    async def scenario(client):
        loop_thread.append(threading.current_thread().name)
        for method in ("initialize", "tools/list", "ping"):
            params = {"protocolVersion": "2025-03-26", "capabilities": {}} if method == "initialize" else {}
            resp = await _post(client, env, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            assert resp.status_code == 200

    _run(scenario)

    assert cold == []
    assert {name for _m, name in seen} == {loop_thread[0]}


def test_unknown_tool_does_not_build_or_wait_behind_a_cold_write(env, cold):
    state = env.state

    async def scenario(client):
        write = asyncio.create_task(_post(client, env, _call("probe_w", {"tag": "a", "hold": True})))
        await _until(state.event("started", "a").is_set, "write probe start")
        unknown = await _post(client, env, _call("no_such_tool", rid=2))
        answered_while_write_ran = not state.event("finished", "a").is_set()
        state.event("release", "a").set()
        await write
        return unknown, answered_while_write_ran

    unknown, early = _run(scenario)

    assert early, "the unknown-tool call waited behind the running write"
    assert "error" in unknown.json()
    assert cold == []


# ---------------------------------------------------------------------------
# T9 -- cold context: only the write thread builds (real _get_context)
# ---------------------------------------------------------------------------

# These waits exist to make the crossing order reachable WITHOUT the rule. With
# the rule the build's wait for ``write_inside`` usually expires, because the
# write probe is queued behind the reader; nothing is asserted about whether it
# does. Only WRITE_LOCK_TIMEOUT is small.
_CROSS_WAIT = 2.0


class _Cold:
    def __init__(self, data_dir: str, *, fail_first: bool) -> None:
        self.data_dir = data_dir
        self.fail_first = fail_first
        self.build_entered = threading.Event()
        self.write_inside = threading.Event()
        self.build_threads: list[str] = []
        self.errors: list[str] = []
        self.builds = 0


@pytest.fixture
def cold_real(tmp_path, monkeypatch):
    """Real ``_get_context`` and ``_context_init_lock``; only ``_build_context``
    is replaced by a build that takes ``write_lock`` the way the store
    constructors do."""
    sql = SQLStore(f"sqlite:///{tmp_path}/opencrab.db")
    user_id, secret = bootstrap_local_user(sql)
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.setenv("WRITE_LOCK_TIMEOUT", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(tools_mod, "_context", {})
    created: list = []

    class Recording(ThreadPoolExecutor):
        def __init__(self, max_workers=None, thread_name_prefix="", **kwargs):
            super().__init__(
                max_workers=max_workers, thread_name_prefix=thread_name_prefix, **kwargs
            )
            self.prefix = thread_name_prefix
            self.width = max_workers
            self.submits = 0
            created.append(self)

        def submit(self, fn, /, *args, **kwargs):
            self.submits += 1
            return super().submit(fn, *args, **kwargs)

    monkeypatch.setattr(http_mod, "ThreadPoolExecutor", Recording, raising=False)
    monkeypatch.setattr(http_mod, "_POOLS", {}, raising=False)
    try:
        yield _Env(_State(), secret, user_id, created), str(tmp_path)
    finally:
        _stop_auth_workers()
        for executor in created:
            executor.shutdown(wait=True)
        get_settings.cache_clear()


def _cold_probes(cold: _Cold, env: _Env):
    from opencrab.locking import write_lock

    def submitted() -> int:
        # Requests handed to the tool pools so far (the auth pool is not counted).
        return env.submits("mcp-write") + env.submits("mcp-read")

    def fake_build():
        cold.builds += 1
        attempt = cold.builds
        cold.build_threads.append(threading.current_thread().name)
        cold.build_entered.set()
        if cold.fail_first and attempt == 1:
            raise RuntimeError("build down")
        # Hold the init lock until the write tool is inside write.lock, then
        # take write.lock the way the store constructors do.
        cold.write_inside.wait(timeout=_CROSS_WAIT)
        with write_lock(cold.data_dir):
            pass
        ctx = {"stub": object()}
        tools_mod._context = ctx
        return ctx

    def get_ctx() -> None:
        try:
            tools_mod._get_context()
        except Exception as exc:  # noqa: BLE001 - recorded, asserted by the test
            cold.errors.append(f"{type(exc).__name__}: {exc}")

    def cold_w() -> dict:
        # Inside write.lock here (dispatch_tool holds it). The baseline is read
        # BEFORE the signal the test waits for, so the reader the test sends
        # afterwards is always counted as a later submission.
        baseline = submitted()
        cold.write_inside.set()
        # Wait until the build has started (reader built on another thread) or
        # until another request reached a tool pool (reader queued behind this
        # write). Giving up earlier would let the write thread build alone
        # before the reader arrives, which passes without the cold-context rule.
        deadline = time.monotonic() + _WAIT
        while not (cold.build_entered.is_set() or submitted() > baseline):
            if time.monotonic() >= deadline:
                cold.errors.append("the second request never reached a tool pool")
                break
            time.sleep(0.01)
        get_ctx()
        return {"w": True}

    def cold_r() -> dict:
        get_ctx()
        return {"r": True}

    return fake_build, cold_w, cold_r


@pytest.mark.parametrize(
    "order,fail_first,batch",
    [
        ("read-first", False, False),
        ("write-first", False, False),
        ("read-first", True, False),
        ("read-first", False, True),
        ("write-first", False, True),
    ],
    ids=["A", "B", "C", "D-read-first", "D-write-first"],
)
def test_cold_context_is_built_by_the_write_thread_only(cold_real, monkeypatch, order, fail_first, batch):
    env, data_dir = cold_real
    cold = _Cold(data_dir, fail_first=fail_first)
    fake_build, cold_w, cold_r = _cold_probes(cold, env)
    monkeypatch.setattr(tools_mod, "_build_context", fake_build)
    tool("cold_w", _SCHEMA, access=AccessTier.WRITE, order=9011, writes=True)(cold_w)
    tool("cold_r", _SCHEMA, access=AccessTier.READ, order=9012, writes=False)(cold_r)

    def read_body(rid):
        item = _call("cold_r", rid=rid)
        return [item] if batch else item

    async def scenario(client):
        if fail_first:
            # The first build raises and leaves the context empty.
            await _post(client, env, read_body(10))
            cold.errors.clear()
            cold.build_entered.clear()
            cold.write_inside.clear()
        if order == "read-first":
            r = asyncio.create_task(_post(client, env, read_body(1)))
            await _until(cold.build_entered.is_set, "build entry")
            w = asyncio.create_task(_post(client, env, _call("cold_w", rid=2)))
        else:
            w = asyncio.create_task(_post(client, env, _call("cold_w", rid=2)))
            await _until(cold.write_inside.is_set, "write inside write.lock")
            r = asyncio.create_task(_post(client, env, read_body(1)))
        return await asyncio.gather(r, w)

    try:
        responses = _run(scenario)
    finally:
        _REGISTRY.pop("cold_w", None)
        _REGISTRY.pop("cold_r", None)

    assert all(resp.status_code == 200 for resp in responses)
    assert cold.errors == [], cold.errors
    # While the context is empty every tool call goes to the write pool: the
    # read pool must not have received a single request.
    assert env.submits("mcp-read") == 0
    assert cold.build_threads and all(name.startswith("mcp-write") for name in cold.build_threads)


# ---------------------------------------------------------------------------
# T8 -- real stores, forced read/write overlap
# ---------------------------------------------------------------------------


class _HookEF:
    """Mock embedding function that proves a read overlapped a write."""

    def __init__(self, dim: int = 1024) -> None:
        self._dim = dim
        self.write_in_embed = threading.Event()
        self.write_done = threading.Event()
        self.overlap = threading.Event()

    def reset(self) -> None:
        for ev in (self.write_in_embed, self.write_done, self.overlap):
            ev.clear()

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        vec = [1.0] + [0.0] * (self._dim - 1)
        if any("writemark" in text for text in input):
            self.write_in_embed.set()
            # Bounded wait for a read to start while this write is in flight.
            self.overlap.wait(timeout=_HOLD)
            self.write_done.set()
        elif self.write_in_embed.is_set() and not self.write_done.is_set():
            self.overlap.set()
        return [vec for _ in input]

    def name(self) -> str:
        return "kure_v1"


@pytest.fixture
def real_env(tmp_path, monkeypatch):
    """Real local stores (sqlite graph/docs/sql/vector) with a hooked mock
    embedding function; the real ``_get_context`` builds them."""
    from opencrab.stores import factory

    sql = SQLStore(f"sqlite:///{tmp_path}/opencrab.db")
    user_id, secret = bootstrap_local_user(sql)
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.setenv("OPENCRAB_STORAGE_MODE", "local")
    get_settings.cache_clear()
    ef = _HookEF()
    monkeypatch.setattr(factory, "_make_kure_embedding_function", lambda settings: ef)
    monkeypatch.setattr(tools_mod, "_context", {})
    yield _Env(_State(), secret, user_id, []), ef
    ctx = tools_mod._context
    for key in ("neo4j", "chroma", "mongo", "sql", "billing"):
        close = getattr(ctx.get(key), "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
    tools_mod._context.clear()
    get_settings.cache_clear()


def _user_args(node_id: str, name: str) -> dict:
    return {
        "space": "subject",
        "node_type": "User",
        "node_id": node_id,
        "properties": {"name": name, "email": "a@ex.com", "role": "admin"},
    }


def test_real_stores_handle_a_read_during_a_write_without_losing_a_leg(real_env):
    env, ef = real_env
    results: list[dict] = []

    async def scenario(client):
        # Prime the context (and the default pack) with one ordinary write.
        prime = await _post(client, env, _call("ontology_add_node", _user_args("prime", "prime-node")))
        assert prime.status_code == 200, prime.text
        results.append(_text(prime))
        for i in range(3):
            ef.reset()
            write = asyncio.create_task(
                _post(client, env, _call("ontology_add_node", _user_args(f"w{i}", f"writemark-{i}"), rid=10 + i))
            )
            await _until(ef.write_in_embed.is_set, f"round {i} write in embedding")
            query = asyncio.create_task(
                _post(client, env, _call("ontology_query",
                                         {"question": f"readmark {i}", "use_rerank": False}, rid=20 + i))
            )
            w_resp, q_resp = await asyncio.gather(write, query)
            assert ef.overlap.is_set(), f"round {i}: the read never overlapped the write"
            assert q_resp.status_code == 200 and "error" not in _text(q_resp), q_resp.text
            results.append(_text(w_resp))
        listing = await _post(client, env, _call("ontology_list_nodes", {"space": "subject", "limit": 50}, rid=99))
        return _text(listing)

    listing = _run(scenario)

    for result in results:
        assert "error" not in result, result
        stores = result["stores"]
        assert stores["graph"] == "ok"
        assert stores["sql"] == "ok"
        assert stores["vector"] == "ok"
        assert stores["audit"] == "ok"
        assert stores["docs"].startswith("ok")
    listed = json.dumps(listing)
    for node_id in ("prime", "w0", "w1", "w2"):
        assert node_id in listed
