"""#302 — ``LlamaCppEmbeddingFunction._get_llm()`` must serialise its lazy
model load.

Without mutual exclusion, two threads that make the very first call both
pass the ``if self._llm is None:`` check and each construct a ``Llama``
instance (a multi-second GGUF load, so the race window is wide in practice).
The REST API app's request handlers dispatch to the thread pool
(``run_in_threadpool`` under a plain ``def`` route, or the sync path FastAPI
uses for non-coroutine handlers), so two concurrent first requests against a
shared embedding function instance are a real, reachable scenario, not a
hypothetical one.

The concurrency test here follows the same discipline as
``tests/test_mcp_context_init_race.py`` (#192): it does NOT assert "thread B
did not finish in time" (that passes on scheduling delay alone). Instead it
installs an instrumented stand-in for the load lock that records the ident
of every acquiring thread, and waits for thread B's own ident to appear.
Every wait carries an upper bound, which is a termination condition for the
harness, not a pass condition.
"""
from __future__ import annotations

import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from opencrab.stores.llamacpp_embedding import LlamaCppEmbeddingFunction

# Generous upper bound, mirroring test_mcp_context_init_race.py's _WAIT.
# Nothing passes *because* of this number -- it only stops the harness from
# hanging when an expectation is already violated (e.g. a reverted fix
# turning the reentrancy guard back into a plain, self-deadlocking Lock).
_WAIT = 30.0


def _install_fake_llama_cpp(monkeypatch, ctor) -> None:
    """Inject a fake ``llama_cpp`` module so ``from llama_cpp import Llama``
    inside ``_get_llm()`` resolves to ``ctor`` without the real dependency
    (not installed in this environment) or a real GGUF load."""
    fake_module = types.ModuleType("llama_cpp")
    fake_module.Llama = ctor
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_module)


def _emb(tmp_path) -> LlamaCppEmbeddingFunction:
    """A GGUF path that already exists, so ``_get_llm()`` never takes the
    auto-download branch (``_ensure_local_gguf``) -- out of scope here."""
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"not a real gguf, never read by the fake Llama ctor")
    return LlamaCppEmbeddingFunction(gguf_path=str(gguf))


class _CtorRecorder:
    """Counts ``Llama(...)`` constructions and can hold the first one open
    indefinitely -- mirrors ``_FactoryRecorder`` in
    ``tests/test_mcp_context_init_race.py``."""

    def __init__(self, *, hold_first: bool = False) -> None:
        self._guard = threading.Lock()
        self.calls = 0
        self.hold_first = hold_first
        self.first_entered = threading.Event()
        self.release_first = threading.Event()

    def __call__(self, **kwargs):  # noqa: ARG002 - Llama(**kwargs) call shape
        with self._guard:
            self.calls += 1
            n = self.calls
        if n == 1 and self.hold_first:
            # Park the first load inside the constructor. This holds the
            # race window open for as long as the test needs, with no
            # sleeps deciding the outcome.
            self.first_entered.set()
            assert self.release_first.wait(timeout=_WAIT), "first ctor never released"
        return MagicMock(name=f"llama-{n}")

    @property
    def call_count(self) -> int:
        with self._guard:
            return self.calls


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
    """Run ``fn`` on a thread and FAIL rather than hang if it never returns.

    A timeout that is silently treated as "did not finish in time, therefore
    pass" would let a reintroduced deadlock (e.g. reverting the reentrancy
    owner-marker to a plain ``Lock``) go undetected in CI -- it would just
    make the run slow, not red. Reaching the bound is asserted as a failure.
    """
    box: dict[str, object] = {}

    def runner() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-asserted by the caller
            box["error"] = exc

    thread = threading.Thread(target=runner, name=name, daemon=True)
    thread.start()
    thread.join(timeout=_WAIT)
    assert not thread.is_alive(), f"{name} never returned -- load deadlocked"
    return box


# ---------------------------------------------------------------------------
# Normal path (also the negative control: no forced contention, must pass
# regardless of whether the lock is present -- proves the assertions below
# are not, by themselves, an artefact of the lock's existence).
# ---------------------------------------------------------------------------

def test_single_thread_loads_exactly_once(tmp_path, monkeypatch) -> None:
    """One caller loads the model once; the next call reuses it."""
    recorder = _CtorRecorder()
    _install_fake_llama_cpp(monkeypatch, recorder)
    emb = _emb(tmp_path)

    first = emb._get_llm()
    second = emb._get_llm()

    assert recorder.call_count == 1
    assert first is second


def test_warm_cache_makes_no_further_lock_acquisitions(tmp_path, monkeypatch) -> None:
    """Once loaded, repeated calls take the fast unlocked read -- no lock
    contention on the hot embedding path."""
    _install_fake_llama_cpp(monkeypatch, lambda **kw: MagicMock(name="llama"))  # noqa: ARG005
    emb = _emb(tmp_path)
    emb._get_llm()  # cold load, acquires the lock once

    probe = _InstrumentedLock()
    monkeypatch.setattr(emb, "_llm_lock", probe)
    for _ in range(5):
        emb._get_llm()

    assert probe.attempts == []


# ---------------------------------------------------------------------------
# The race
# ---------------------------------------------------------------------------

def test_concurrent_first_calls_load_exactly_once(tmp_path, monkeypatch) -> None:
    """Two simultaneous first calls load the model once and share the
    instance -- forced via a barrier-style hold/release pair, not by
    hoping the scheduler interleaves them (the required negative-control
    discipline: unforced concurrency could pass without the lock too)."""
    recorder = _CtorRecorder(hold_first=True)
    _install_fake_llama_cpp(monkeypatch, recorder)
    emb = _emb(tmp_path)
    probe = _InstrumentedLock()
    monkeypatch.setattr(emb, "_llm_lock", probe)

    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def worker(tag: str) -> None:
        try:
            results[tag] = emb._get_llm()
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors[tag] = exc

    thread_a = threading.Thread(target=worker, args=("A",), name="llm-A", daemon=True)
    thread_b = threading.Thread(target=worker, args=("B",), name="llm-B", daemon=True)

    thread_a.start()
    try:
        assert recorder.first_entered.wait(timeout=_WAIT), "A never reached the constructor"
        thread_b.start()

        # Positive observation: wait until B's own ident shows up among the
        # lock acquisitions. Reverting the fix removes the lock entirely, so
        # B's ident never appears and this fails deterministically.
        deadline = time.monotonic() + _WAIT
        while not probe.saw(thread_b.ident):
            assert time.monotonic() < deadline, (
                "thread B never tried to acquire the load lock -- "
                "_get_llm() is not serialising its initialisation"
            )
            time.sleep(0.01)

        # B is parked on the lock, so it cannot have reached the constructor.
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


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_failed_load_releases_the_lock_and_retries(tmp_path, monkeypatch) -> None:
    """A load error propagates, leaves no cached model, and does not strand
    the lock for the next caller."""
    attempts = {"n": 0}

    def flaky(**kwargs):  # noqa: ARG001 - Llama(**kwargs) call shape
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("model load failed")
        return MagicMock(name="llama-retry")

    _install_fake_llama_cpp(monkeypatch, flaky)
    emb = _emb(tmp_path)

    with pytest.raises(RuntimeError, match="model load failed"):
        emb._get_llm()
    assert emb._llm is None
    assert emb._llm_init_owner is None

    # Bounded, so a stranded lock fails the test instead of hanging it.
    box = _run_bounded(emb._get_llm, name="llm-retry")
    assert "error" not in box, box.get("error")
    assert box["value"] is not None
    assert attempts["n"] == 2


def test_same_thread_reentrant_load_fails_fast(tmp_path, monkeypatch) -> None:
    """A caller re-entering _get_llm() on its own thread raises, not
    deadlocks.

    No caller in this codebase does this today (verified across
    opencrab/); this pins the contract the owner-marker exists for. A plain
    ``Lock`` without the marker would deadlock the reentrant call forever --
    exactly what ``_run_bounded``'s explicit timeout turns into an observed
    test FAILURE instead of a CI process that never returns.
    """
    emb = _emb(tmp_path)

    def reentrant_ctor(**kwargs):  # noqa: ARG001 - Llama(**kwargs) call shape
        return emb._get_llm()

    _install_fake_llama_cpp(monkeypatch, reentrant_ctor)

    box = _run_bounded(emb._get_llm, name="llm-reentrant")
    error = box.get("error")
    assert isinstance(error, RuntimeError), f"expected RuntimeError, got {error!r}"
    assert "reentrant" in str(error).lower()
    assert emb._llm is None
    assert emb._llm_init_owner is None


# ---------------------------------------------------------------------------
# Per-context uniqueness (#302 team-lead requirement 4): the fix guarantees
# uniqueness of the embedding function WITHIN one context (one instance-level
# lock per instance), not one shared instance per process. A combined
# deployment (apps/api/main.py mounting mcp_router() alongside its own REST
# routes) builds two independent contexts -- REST's ApiContext and MCP's
# _get_context() -- and each must get its own vector store and its own
# embedding function. A future change that accidentally makes
# _make_kure_embedding_function() process-wide-cached would silently narrow
# that contract; this guards against it at the actual embedding-function
# object-identity level, not just the vector-store wrapper's.
# ---------------------------------------------------------------------------

def test_two_contexts_get_independent_embedding_function_instances(tmp_path) -> None:
    from opencrab.config import Settings
    from opencrab.stores.factory import make_vector_store

    settings = Settings(
        LOCAL_DATA_DIR=str(tmp_path),
        STORAGE_MODE="local",
        EMBEDDING_BACKEND="openai",
    )
    assert settings.vector_backend_resolved == "sqlite-vec"

    store_a = make_vector_store(settings)
    store_b = make_vector_store(settings)
    try:
        assert store_a is not store_b
        assert store_a._ef is not store_b._ef
        # Walk down to the actual LlamaCppEmbeddingFunction object identity
        # (ResilientEmbeddingFunction._fallback), not just the wrapper's --
        # a future factory change could cache the wrapper while still
        # constructing two distinct fallback EFs, or vice versa.
        assert store_a._ef._fallback is not store_b._ef._fallback
    finally:
        for store in (store_a, store_b):
            close = getattr(store, "close", None)
            if callable(close):
                close()
