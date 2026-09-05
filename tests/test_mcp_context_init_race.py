"""#192 — ``_get_context()`` must serialise its lazy initialisation.

Without mutual exclusion, two threads that make the very first call both pass
the ``if _context:`` check and each run the store factories. The process then
holds two store instances pointing at one backend, and every adapter's
instance-level lock stops serialising anything, because the two instances do
not share a lock.

The concurrency test here does NOT assert "thread B did not finish in time".
Such an assertion passes on scheduling delay alone and so loses its power to
detect a reverted fix. Instead it installs an instrumented stand-in for the
initialisation lock that records the ident of every acquiring thread, and
waits for thread B's own ident to appear. Every wait carries an upper bound,
which is a termination condition for the harness rather than a pass condition.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

import opencrab.config as config_mod
import opencrab.stores.factory as factory_mod
from opencrab.mcp import tools as tools_mod

# Generous upper bound. Nothing passes *because* of this number -- it only
# stops the harness from hanging when an expectation is already violated.
_WAIT = 30.0

_CONTEXT_KEYS = {
    "neo4j", "chroma", "mongo", "sql",
    "builder", "rebac", "impact", "hybrid", "billing",
}


@pytest.fixture
def isolated_context(tmp_path, monkeypatch):
    """Point the settings at a scratch dir and reset the context around a test."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    config_mod.get_settings.cache_clear()
    # The chroma shared flock is a cross-process concern (#140/#141) and not
    # what this module measures.
    tools_mod._context.clear()
    try:
        yield
    finally:
        tools_mod._context.clear()
        config_mod.get_settings.cache_clear()


class _FactoryRecorder:
    """Counts graph-store builds and can hold the first one open indefinitely."""

    def __init__(self, *, hold_first: bool = False) -> None:
        self._guard = threading.Lock()
        self.calls = 0
        self.hold_first = hold_first
        self.first_entered = threading.Event()
        self.release_first = threading.Event()

    def make_graph_store(self, cfg):  # noqa: ARG002 - factory signature
        with self._guard:
            self.calls += 1
            n = self.calls
        if n == 1 and self.hold_first:
            # Park the first initialiser inside the factory. This holds the
            # race window open for as long as the test needs, with no sleeps
            # deciding the outcome.
            self.first_entered.set()
            assert self.release_first.wait(timeout=_WAIT), "first factory never released"
        return MagicMock(name=f"graph-{n}")

    @property
    def call_count(self) -> int:
        with self._guard:
            return self.calls


def _install_factories(monkeypatch, recorder: _FactoryRecorder) -> None:
    monkeypatch.setattr(factory_mod, "make_graph_store", recorder.make_graph_store)
    for name in ("make_vector_store", "make_doc_store", "make_sql_store"):
        monkeypatch.setattr(factory_mod, name, lambda cfg, _n=name: MagicMock(name=_n))
    monkeypatch.setattr(
        factory_mod, "make_billing_sql_store", lambda cfg, sql: MagicMock(name="billing")
    )


class _InstrumentedLock:
    """Duck-typed stand-in recording the ident of every acquiring thread.

    A single "someone acquired" event would be set by thread A's own first
    acquisition and so could never prove that thread B arrived. Recording
    idents keeps the two threads distinguishable.
    """

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

    def saw(self, ident: int | None) -> bool:
        with self._guard:
            return ident in self.attempts


def _run_bounded(fn, *, name: str):
    """Run ``fn`` on a thread and fail rather than hang if it never returns."""
    box: dict[str, object] = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-asserted by the caller
            box["error"] = exc

    thread = threading.Thread(target=runner, name=name, daemon=True)
    thread.start()
    thread.join(timeout=_WAIT)
    assert not thread.is_alive(), f"{name} never returned -- initialisation deadlocked"
    return box


# ---------------------------------------------------------------------------
# Normal path
# ---------------------------------------------------------------------------

def test_single_thread_initialises_exactly_once(isolated_context, monkeypatch):
    """One caller builds the context once; the next call reuses it."""
    recorder = _FactoryRecorder()
    _install_factories(monkeypatch, recorder)

    first = tools_mod._get_context()
    second = tools_mod._get_context()

    assert recorder.call_count == 1
    assert first is second
    assert set(first) == _CONTEXT_KEYS


# ---------------------------------------------------------------------------
# The race
# ---------------------------------------------------------------------------

def test_concurrent_first_calls_share_one_context(isolated_context, monkeypatch):
    """Two simultaneous first calls run the factories once and share instances."""
    recorder = _FactoryRecorder(hold_first=True)
    _install_factories(monkeypatch, recorder)
    probe = _InstrumentedLock()
    monkeypatch.setattr(tools_mod, "_context_init_lock", probe)

    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def worker(tag: str) -> None:
        try:
            results[tag] = tools_mod._get_context()
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors[tag] = exc

    thread_a = threading.Thread(target=worker, args=("A",), name="ctx-A", daemon=True)
    thread_b = threading.Thread(target=worker, args=("B",), name="ctx-B", daemon=True)

    thread_a.start()
    try:
        assert recorder.first_entered.wait(timeout=_WAIT), "A never reached the factory"
        thread_b.start()

        # Positive observation: wait until B's own ident shows up among the
        # lock acquisitions. Reverting the fix removes the lock entirely, so
        # B's ident never appears and this fails deterministically.
        deadline = time.monotonic() + _WAIT
        while not probe.saw(thread_b.ident):
            assert time.monotonic() < deadline, (
                "thread B never tried to acquire the initialisation lock -- "
                "_get_context() is not serialising its initialisation"
            )
            time.sleep(0.01)

        # B is parked on the lock, so it cannot have reached the factory.
        assert recorder.call_count == 1
    finally:
        recorder.release_first.set()

    thread_a.join(timeout=_WAIT)
    thread_b.join(timeout=_WAIT)
    assert not thread_a.is_alive(), "thread A never finished"
    assert not thread_b.is_alive(), "thread B never finished"
    assert errors == {}

    assert recorder.call_count == 1
    assert results["A"] is results["B"]
    assert results["A"]["neo4j"] is results["B"]["neo4j"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_failed_initialisation_releases_the_lock_and_retries(isolated_context, monkeypatch):
    """A factory error propagates, leaves no context, and does not strand the lock."""
    recorder = _FactoryRecorder()
    _install_factories(monkeypatch, recorder)
    attempts = {"n": 0}

    def flaky(cfg):  # noqa: ARG001 - factory signature
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("factory down")
        return MagicMock(name="graph-retry")

    monkeypatch.setattr(factory_mod, "make_graph_store", flaky)

    with pytest.raises(RuntimeError, match="factory down"):
        tools_mod._get_context()
    assert tools_mod._context == {}

    # Bounded, so a stranded lock fails the test instead of hanging it.
    box = _run_bounded(tools_mod._get_context, name="ctx-retry")
    assert "error" not in box, box.get("error")
    assert set(box["value"]) == _CONTEXT_KEYS
    assert attempts["n"] == 2


def test_same_thread_reentrant_initialisation_fails_fast(isolated_context, monkeypatch):
    """A factory re-entering _get_context() on its own thread raises, not deadlocks.

    Only same-thread re-entry is detectable here. A factory that delegates to a
    child thread and waits for it still deadlocks, by design -- see the
    ``_context_init_owner`` comment in the module under test.
    """
    recorder = _FactoryRecorder()
    _install_factories(monkeypatch, recorder)
    monkeypatch.setattr(factory_mod, "make_graph_store", lambda cfg: tools_mod._get_context())

    box = _run_bounded(tools_mod._get_context, name="ctx-reentrant")
    error = box.get("error")
    assert isinstance(error, RuntimeError), f"expected RuntimeError, got {error!r}"
    assert "reentrant" in str(error).lower()
    assert tools_mod._context == {}
    assert tools_mod._context_init_owner is None


def test_build_context_refuses_a_caller_without_the_lock(isolated_context, monkeypatch):
    """_build_context() is internal: calling it directly must not rerun the factories."""
    recorder = _FactoryRecorder()
    _install_factories(monkeypatch, recorder)

    with pytest.raises(RuntimeError, match="without holding the context"):
        tools_mod._build_context()

    assert recorder.call_count == 0
    assert tools_mod._context == {}

    # The refusal is about the lock, not about the function being broken.
    assert set(tools_mod._get_context()) == _CONTEXT_KEYS
    assert recorder.call_count == 1


def test_ownership_marker_is_cleared_after_a_failed_initialisation(
    isolated_context, monkeypatch
):
    """A failed build leaves no owner behind for a later thread to impersonate."""
    recorder = _FactoryRecorder()
    _install_factories(monkeypatch, recorder)

    def exploding(cfg):  # noqa: ARG001 - factory signature
        raise RuntimeError("factory down")

    monkeypatch.setattr(factory_mod, "make_graph_store", exploding)

    with pytest.raises(RuntimeError, match="factory down"):
        tools_mod._get_context()

    assert tools_mod._context_init_owner is None
    # A stale marker would let this bypass the ownership check.
    with pytest.raises(RuntimeError, match="without holding the context"):
        tools_mod._build_context()
