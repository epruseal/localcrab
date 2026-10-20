"""#508 -- ``LlamaCppEmbeddingFunction`` releases the GGUF model after an idle
period and reloads it on the next call.

Design: docs/issue-508-gguf-fallback-release.md. The tests use a fake ``Llama``
(counts loads and closes, can park inside ``create_embedding`` or ``close``), a
fake clock and a fake timer factory. Order is forced with events and an
instrumented lock; every wait has an upper bound that only stops the harness
from hanging. Time never decides a verdict, except the one real-timer test.
"""
from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from opencrab.stores import llamacpp_embedding
from opencrab.stores.llamacpp_embedding import LlamaCppEmbeddingFunction

_WAIT = 30.0
IDLE = 120.0


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeTimer:
    def __init__(self, delay, fn) -> None:
        self.delay = delay
        self.fn = fn
        self.started = False
        self.fired = False

    def start(self) -> None:
        self.started = True

    def fire(self) -> None:
        self.fired = True
        self.fn()


class _Timers:
    def __init__(self) -> None:
        self.created: list[_FakeTimer] = []

    def __call__(self, delay, fn) -> _FakeTimer:
        timer = _FakeTimer(delay, fn)
        self.created.append(timer)
        return timer

    @property
    def pending(self) -> list[_FakeTimer]:
        return [t for t in self.created if not t.fired]


class _Model:
    """Fake ``Llama``. The vector depends only on the input text."""

    def __init__(self, world: _World, index: int) -> None:
        self.world = world
        self.index = index
        self.closed = False

    def create_embedding(self, text: str):
        with self.world.guard:
            if self.closed:
                self.world.used_after_close += 1
        if self.world.on_embed is not None:
            self.world.on_embed(text)
        with self.world.guard:
            # A close that lands while the call is inside the model also counts.
            if self.closed:
                self.world.used_after_close += 1
        return {"data": [{"embedding": [float(len(text)), 1.0, 2.0]}]}

    def close(self) -> None:
        self.world.events.append(f"close-start-{self.index}")
        if self.world.on_close is not None:
            self.world.on_close(self)
        with self.world.guard:
            self.closed = True
            self.world.open_now -= 1
        self.world.events.append(f"close-end-{self.index}")
        if self.world.close_raises:
            raise RuntimeError("close failed")


class _World:
    """Everything the fake ``Llama`` constructor and its models record."""

    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.models: list[_Model] = []
        self.events: list[str] = []
        self.open_now = 0
        self.max_open = 0
        self.used_after_close = 0
        self.on_embed = None
        self.on_close = None
        self.on_ctor = None
        self.close_raises = False

    def __call__(self, **kwargs) -> _Model:  # noqa: ARG002 - Llama(**kwargs)
        with self.guard:
            model = _Model(self, len(self.models) + 1)
            self.models.append(model)
            self.open_now += 1
            self.max_open = max(self.max_open, self.open_now)
        self.events.append(f"ctor-{model.index}")
        if self.on_ctor is not None:
            self.on_ctor(model)
        return model


class _InstrumentedLock:
    """Records the ident of every acquiring thread (same idea as the #302 test)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self.attempts: list[int] = []

    def acquire(self, *args, **kwargs):
        with self._guard:
            self.attempts.append(threading.get_ident())
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def saw(self, ident) -> bool:
        with self._guard:
            return ident in self.attempts


@pytest.fixture
def env(tmp_path, monkeypatch):
    world = _World()
    fake_module = types.ModuleType("llama_cpp")
    fake_module.Llama = world
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_module)
    # glibc heap trim is irrelevant here and must not run in unit tests.
    monkeypatch.setattr(llamacpp_embedding, "_trim_heap", lambda: world.events.append("trim"))
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"not a real gguf")
    clock = _Clock()
    timers = _Timers()

    def make(idle: float = IDLE, *, real_timer: bool = False, **kw) -> LlamaCppEmbeddingFunction:
        if real_timer:
            return LlamaCppEmbeddingFunction(gguf_path=str(gguf), idle_unload_seconds=idle, **kw)
        emb = LlamaCppEmbeddingFunction(
            gguf_path=str(gguf), idle_unload_seconds=idle, clock=clock, **kw
        )
        emb._timer_factory = timers
        return emb

    return types.SimpleNamespace(world=world, clock=clock, timers=timers, make=make)


def _start(fn, name: str) -> threading.Thread:
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - asserted by the caller
            box["error"] = exc

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.box = box  # type: ignore[attr-defined]
    thread.start()
    return thread


def _join(thread: threading.Thread) -> dict:
    thread.join(timeout=_WAIT)
    assert not thread.is_alive(), f"{thread.name} never finished"
    return thread.box  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Normal path
# ---------------------------------------------------------------------------

def test_release_happens_only_after_idle_time(env) -> None:
    emb = env.make()
    emb(["a"])
    assert emb.loaded

    env.clock.now = IDLE - 1
    assert emb.release_if_idle() is False
    assert not env.world.models[0].closed
    assert emb.loaded

    env.clock.now = IDLE
    assert emb.release_if_idle() is True
    assert env.world.models[0].closed
    assert not emb.loaded
    assert emb.release_count == 1
    # close() runs before the heap trim.
    assert env.world.events.index("close-end-1") < env.world.events.index("trim")


def test_reload_after_release_returns_the_same_vectors(env) -> None:
    emb = env.make()
    first = emb(["abc", "de"])
    env.clock.now = IDLE
    assert emb.release_if_idle() is True

    second = emb(["abc", "de"])

    assert first == second
    assert emb.load_count == 2
    assert len(env.world.models) == 2
    assert env.world.models[0].closed and not env.world.models[1].closed


def test_return_of_a_lease_restarts_the_idle_period(env) -> None:
    emb = env.make()
    env.clock.now = 100.0
    emb(["a"])  # last use at t=100

    env.clock.now = 100.0 + IDLE - 1
    assert emb.release_if_idle() is False
    env.clock.now = 100.0 + IDLE
    assert emb.release_if_idle() is True


# ---------------------------------------------------------------------------
# Release never overlaps a use
# ---------------------------------------------------------------------------

def test_no_release_while_a_call_is_in_flight(env) -> None:
    entered, proceed = threading.Event(), threading.Event()

    def on_embed(text: str) -> None:
        entered.set()
        assert proceed.wait(timeout=_WAIT)

    env.world.on_embed = on_embed
    emb = env.make()
    worker = _start(lambda: emb(["a"]), "inflight")
    try:
        assert entered.wait(timeout=_WAIT)
        env.clock.now = IDLE * 10
        # The release decision really ran and answered no.
        assert emb.release_if_idle() is False
        assert not env.world.models[0].closed
    finally:
        proceed.set()
    assert "error" not in _join(worker)

    # The lease return refreshed the last-use time, so the model is not due yet.
    assert emb.release_if_idle() is False
    env.clock.now += IDLE
    assert emb.release_if_idle() is True


def test_a_thread_that_waited_for_the_load_also_holds_a_lease(env, monkeypatch) -> None:
    """A and B both start cold. A loads, B waits on the load lock and then finds
    the model already loaded. After A has returned its lease, B is still in
    ``create_embedding`` and the model must not be due for release."""
    ctor_entered, ctor_proceed = threading.Event(), threading.Event()
    b_in_embed, b_proceed = threading.Event(), threading.Event()

    def on_ctor(model) -> None:
        ctor_entered.set()
        assert ctor_proceed.wait(timeout=_WAIT)

    def on_embed(text: str) -> None:
        if text == "B":
            b_in_embed.set()
            assert b_proceed.wait(timeout=_WAIT)

    env.world.on_ctor = on_ctor
    env.world.on_embed = on_embed
    emb = env.make()
    probe = _InstrumentedLock()
    monkeypatch.setattr(emb, "_llm_lock", probe)

    thread_a = _start(lambda: emb(["A"]), "A")
    assert ctor_entered.wait(timeout=_WAIT)
    thread_b = _start(lambda: emb(["B"]), "B")
    try:
        deadline = time.monotonic() + _WAIT
        while not probe.saw(thread_b.ident):
            assert time.monotonic() < deadline, "B never tried the load lock"
            time.sleep(0.01)
    finally:
        ctor_proceed.set()
    assert "error" not in _join(thread_a)
    try:
        assert b_in_embed.wait(timeout=_WAIT)
        assert emb._inflight == 1  # only B
        env.clock.now = IDLE * 10
        assert emb.release_if_idle() is False
        assert not env.world.models[0].closed
    finally:
        b_proceed.set()
    assert "error" not in _join(thread_b)
    assert emb._inflight == 0
    assert len(env.world.models) == 1


def test_call_arriving_during_close_waits_and_loads_after_it(env, monkeypatch) -> None:
    in_close, finish_close = threading.Event(), threading.Event()

    def on_close(model) -> None:
        in_close.set()
        assert finish_close.wait(timeout=_WAIT)

    env.world.on_close = on_close
    emb = env.make()
    emb(["a"])
    probe = _InstrumentedLock()
    monkeypatch.setattr(emb, "_llm_lock", probe)

    env.clock.now = IDLE
    releaser = _start(emb.release_if_idle, "releaser")
    assert in_close.wait(timeout=_WAIT)
    caller = _start(lambda: emb(["b"]), "caller")
    try:
        deadline = time.monotonic() + _WAIT
        while not probe.saw(caller.ident):
            assert time.monotonic() < deadline, "caller never tried the lock"
            time.sleep(0.01)
        # The caller is parked on the lock: no second model exists yet.
        assert len(env.world.models) == 1
    finally:
        finish_close.set()
    assert _join(releaser)["value"] is True
    assert "error" not in _join(caller)

    assert env.world.events.index("close-end-1") < env.world.events.index("ctor-2")
    assert env.world.max_open == 1
    assert env.world.used_after_close == 0


def test_concurrent_calls_and_releases_never_overlap(env) -> None:
    class _AlwaysDue:
        """Every clock read moves far past the idle period."""

        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.now = 0.0

        def __call__(self) -> float:
            with self.lock:
                self.now += IDLE
                return self.now

    clock = _AlwaysDue()
    emb = env.make()
    emb._clock = clock
    # Yield the interpreter inside every embedding so that releases land both
    # between calls and while a call holds its lease.
    env.world.on_embed = lambda text: time.sleep(0)
    stop = threading.Event()
    results: list = []
    attempts = {"n": 0}

    def caller() -> None:
        while not stop.is_set():
            results.append(emb(["xyz"]))
            time.sleep(0)

    def releaser() -> None:
        while not stop.is_set():
            emb.release_if_idle()
            attempts["n"] += 1
            time.sleep(0)

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)
    try:
        callers = [_start(caller, f"caller-{i}") for i in range(4)]
        rel = _start(releaser, "releaser")
        # Termination condition of the harness: enough calls AND enough real
        # releases happened while the callers were running. The verdict is the
        # invariants below, not this wait.
        deadline = time.monotonic() + _WAIT
        while len(results) < 600 or emb.release_count < 20:
            assert time.monotonic() < deadline, (
                f"not enough overlap: calls={len(results)} releases={emb.release_count}"
            )
            time.sleep(0.005)
        stop.set()
        for thread in callers:
            assert "error" not in _join(thread)
        assert "error" not in _join(rel)
    finally:
        stop.set()
        sys.setswitchinterval(old_interval)

    assert attempts["n"] >= emb.release_count >= 20
    assert all(r == results[0] for r in results)
    assert env.world.used_after_close == 0
    assert env.world.max_open == 1
    assert emb._inflight == 0
    assert emb.load_count == emb.release_count + (1 if emb.loaded else 0)


# ---------------------------------------------------------------------------
# Timer
# ---------------------------------------------------------------------------

def test_a_lease_return_arms_one_timer_and_its_firing_releases(env) -> None:
    emb = env.make()
    emb(["a"])
    emb(["a"])
    assert len(env.timers.created) == 1
    assert env.timers.created[0].delay == IDLE
    assert env.timers.created[0].started
    assert emb._timer is env.timers.created[0]

    env.clock.now = IDLE
    env.timers.created[0].fire()

    assert not emb.loaded
    assert emb._timer is None
    assert env.timers.pending == []
    assert emb.release_count == 1


def test_a_timer_that_fires_early_rearms_for_the_remaining_time(env) -> None:
    emb = env.make()
    emb(["a"])
    env.clock.now = 30.0
    env.timers.created[0].fire()

    assert emb.loaded
    assert len(env.timers.pending) == 1
    assert env.timers.pending[0].delay == pytest.approx(IDLE - 30.0)
    assert emb._timer is env.timers.pending[0]


def test_lease_return_during_a_running_timer_leaves_one_pending_timer(env, monkeypatch) -> None:
    """The timer callback is parked before its decision section while a call
    returns its lease. The call must not arm a second timer, and the callback's
    own re-arm must be the only pending timer afterwards."""
    emb = env.make()
    emb(["a"])
    first = env.timers.created[0]

    gate = threading.Lock()
    gate.acquire()
    probe = _InstrumentedLock()
    probe._lock = gate  # held by the test: the callback parks on it
    monkeypatch.setattr(emb, "_llm_lock", probe)

    env.clock.now = 50.0
    callback = _start(first.fire, "timer-callback")
    deadline = time.monotonic() + _WAIT
    while not probe.saw(callback.ident):
        assert time.monotonic() < deadline, "callback never reached the lock"
        time.sleep(0.01)

    emb(["a"])  # lease return while the callback is waiting
    assert len(env.timers.created) == 1  # no second timer yet

    gate.release()
    assert "error" not in _join(callback)

    assert len(env.timers.created) == 2
    assert env.timers.pending == [env.timers.created[1]]
    assert emb._timer is env.timers.created[1]
    assert emb.loaded


def test_timer_firing_while_a_call_is_in_flight_does_not_release(env) -> None:
    emb = env.make()
    emb(["a"])  # arms the first timer
    first = env.timers.created[0]

    entered, proceed = threading.Event(), threading.Event()

    def on_embed(text: str) -> None:
        entered.set()
        assert proceed.wait(timeout=_WAIT)

    env.world.on_embed = on_embed
    decisions: list[bool] = []
    real = emb.release_if_idle

    def recording(*args, **kwargs) -> bool:
        decisions.append(real(*args, **kwargs))
        return decisions[-1]

    emb.release_if_idle = recording  # type: ignore[method-assign]
    worker = _start(lambda: emb(["b"]), "inflight")
    try:
        assert entered.wait(timeout=_WAIT)
        env.clock.now = IDLE * 10
        first.fire()
        # The release decision ran (recorded) and answered no.
        assert decisions == [False]
        assert not env.world.models[0].closed
        assert emb.loaded
        assert len(env.timers.pending) == 1  # re-armed
    finally:
        proceed.set()
    assert "error" not in _join(worker)


def test_real_timer_releases_after_a_short_idle_period(env) -> None:
    closed = threading.Event()
    env.world.on_close = lambda model: closed.set()
    emb = env.make(0.05, real_timer=True)
    emb(["a"])
    assert closed.wait(timeout=_WAIT), "the real timer never released the model"
    deadline = time.monotonic() + _WAIT
    while emb.loaded or emb._timer is not None:
        assert time.monotonic() < deadline, "state did not settle"
        time.sleep(0.01)
    assert emb.release_count == 1


def test_idle_unload_disabled_by_non_positive_value(env) -> None:
    emb = env.make(idle=0)
    emb(["a"])
    env.clock.now = 10_000
    assert env.timers.created == []
    assert emb.release_if_idle() is False
    assert emb.loaded
    assert not env.world.models[0].closed


# ---------------------------------------------------------------------------
# Failure and re-entry
# ---------------------------------------------------------------------------

def test_failed_close_leaves_the_function_usable(env) -> None:
    env.world.close_raises = True
    emb = env.make()
    emb(["a"])
    env.clock.now = IDLE

    assert emb.release_if_idle() is True  # no exception reaches the caller
    assert not emb.loaded
    assert emb.release_count == 0
    assert emb.release_error_count == 1

    env.world.close_raises = False
    emb(["a"])
    assert emb.loaded
    assert len(env.world.models) == 2


def test_release_requested_during_load_returns_false(env) -> None:
    box: dict = {}

    def on_ctor(model) -> None:
        box["answer"] = emb.release_if_idle()

    env.world.on_ctor = on_ctor
    emb = env.make()
    result = _start(lambda: emb(["a"]), "load")
    assert "error" not in _join(result)
    assert box["answer"] is False
    assert emb.loaded


def test_call_from_inside_close_fails_fast_instead_of_deadlocking(env) -> None:
    box: dict = {}

    def on_close(model) -> None:
        try:
            emb(["x"])
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            box["error"] = exc

    env.world.on_close = on_close
    emb = env.make()
    emb(["a"])
    env.clock.now = IDLE
    releaser = _start(emb.release_if_idle, "releaser")
    assert _join(releaser)["value"] is True
    assert isinstance(box.get("error"), RuntimeError)
    assert "reentrant" in str(box["error"]).lower()
