"""#573 최소 관측 계약과 고의 손상 검출을 검사한다."""
import asyncio
import json
import os
import subprocess
import sys
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencrab.observability import search_trace as obs
from opencrab.observability.search_trace import (
    Trace,
    observe,
    observed,
    request_span,
    span,
    validate,
)


def test_nested_trace_and_terminal_error():
    trace = Trace("synthetic")
    with observe(trace), request_span():
        with span("query"):
            pass
    assert validate(trace.document(), expected_mode="general") == []
    damaged = deepcopy(trace.document())
    damaged["events"].pop()
    assert "unpaired" in validate(damaged, expected_mode="general")


@pytest.mark.parametrize("failure,status", [
    (None, "ok"), (RuntimeError("body"), "error"),
    (asyncio.CancelledError("body"), "cancelled"),
    (KeyboardInterrupt("body"), "cancelled"), (SystemExit("body"), "cancelled"),
])
def test_request_span_status_and_context(failure, status):
    trace = Trace("request-status")
    with observe(trace), request_span() as outer:
        if failure is None:
            with request_span() as inner:
                pass
        else:
            with pytest.raises(type(failure)) as caught:
                with request_span() as inner:
                    raise failure
            assert caught.value is failure
        with span("after"):
            pass
    end = next(event for event in trace.events if event["kind"] == "end" and event["span_id"] == inner)
    after = next(event for event in trace.events if event["kind"] == "start" and event["operation"] == "after")
    assert end["status"] == status
    assert after["request_id"] == outer and after["parent_id"] == outer
    assert validate(trace.document(), expected_mode="general") == []


@pytest.mark.parametrize("outer_type", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("body_type,status", [(None, "ok"), (RuntimeError, "error"),
                                              (asyncio.CancelledError, "cancelled")])
def test_request_span_outer_exception_context(outer_type, body_type, status):
    trace = Trace("outer-context")
    outer = outer_type("outer")
    body = body_type("body") if body_type else None
    with observe(trace):
        before = (obs._stack.get(), obs._request.get())
        try:
            raise outer
        except outer_type:
            if body is None:
                with request_span():
                    pass
            else:
                with pytest.raises(body_type) as caught:
                    with request_span():
                        raise body
                assert caught.value is body
            assert (obs._stack.get(), obs._request.get()) == before
    end = next(event for event in trace.events if event["operation"] == "request" and event["kind"] == "end")
    assert end["status"] == status


@pytest.mark.parametrize("outer_type", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("phase", ["start-clock", "start-emit", "end-clock", "end-emit", "reset"])
@pytest.mark.parametrize("body_type", [None, RuntimeError, asyncio.CancelledError])
def test_request_span_nested_failures(monkeypatch, outer_type, phase, body_type):
    trace = Trace("nested-failure")
    outer = outer_type("outer")
    body = body_type("body") if body_type else None
    observer = asyncio.CancelledError("observer")
    reset_error = RuntimeError("reset")
    reset_tokens = obs._reset_tokens
    clock = obs.time.perf_counter
    emit = trace.emit
    markers = []
    body_entered = []
    observe_passed = []

    def injected_clock():
        if sys._getframe(1).f_code is obs.request_span.__wrapped__.__code__:
            target = "end-clock" if body_entered else "start-clock"
            if phase == target:
                markers.append(target)
                raise observer
        return clock()

    def injected_emit(kind, operation, **fields):
        target = f"{kind}-emit"
        if operation == "request" and phase == target:
            markers.append(target)
            raise observer
        return emit(kind, operation, **fields)

    class BadReset:
        def reset(self, token):
            markers.append("reset")
            raise reset_error

    def injected_reset(tokens, sink, original=obs._OMITTED):
        caller = sys._getframe(1).f_code
        if sink is trace and caller is obs.RequestTimer.close.__code__ and any(variable is obs._request for variable, _ in tokens):
            tokens = [(BadReset(), None), *tokens]
        elif sink is trace and caller is obs.observe.__wrapped__.__code__:
            observe_passed.append(True)
        return reset_tokens(tokens, sink, original)

    monkeypatch.setattr(obs.time, "perf_counter", injected_clock)
    monkeypatch.setattr(trace, "emit", injected_emit)
    monkeypatch.setattr(obs, "_reset_tokens", injected_reset)
    expected = observer if phase in ("start-clock", "start-emit") else (body or (reset_error if phase == "reset" else observer))
    with observe(trace):
        before = (obs._stack.get(), obs._request.get())
        try:
            raise outer
        except outer_type:
            with pytest.raises(type(expected)) as caught:
                with request_span():
                    body_entered.append(True)
                    if body is not None:
                        raise body
            assert caught.value is expected
            assert (obs._stack.get(), obs._request.get()) == before
    assert bool(body_entered) == (phase not in ("start-clock", "start-emit"))
    assert markers.count("reset") == 1
    if phase != "reset":
        assert markers.count(phase) == 1
    assert observe_passed and trace.failed


def test_observe_reset_failure_propagates(monkeypatch):
    trace = Trace("observe-reset")
    reset_tokens = obs._reset_tokens
    failure = RuntimeError("observe reset")

    class BadReset:
        def reset(self, token):
            raise failure

    def injected_reset(tokens, sink, original=obs._OMITTED):
        if sink is trace and sys._getframe(1).f_code is obs.observe.__wrapped__.__code__:
            tokens = [(BadReset(), None), *tokens]
        return reset_tokens(tokens, sink, original)

    monkeypatch.setattr(obs, "_reset_tokens", injected_reset)
    with pytest.raises(RuntimeError) as caught:
        with observe(trace):
            pass
    assert caught.value is failure and trace.failed


@pytest.mark.parametrize("outer_type", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("boundary", ["observe", "metadata", "span", "phase"])
@pytest.mark.parametrize("body_fails", [False, True])
def test_shared_context_body_error_precedes_reset(monkeypatch, outer_type, boundary, body_fails):
    trace = Trace("shared-reset")
    body = RuntimeError("body")
    reset_error = RuntimeError("reset")
    original = obs._reset_tokens
    reached = []
    selected = []
    passed = []
    manager = {"observe": obs.observe, "metadata": obs.metadata, "span": obs.span, "phase": obs.phase}[boundary]
    code = (obs.span if boundary == "phase" else manager).__wrapped__.__code__
    token_variable = {"observe": obs._request, "metadata": obs._meta,
                      "span": obs._stack, "phase": obs._stack}[boundary]
    class BadReset:
        def reset(self, token):
            reached.append(True)
            raise reset_error
    def injected(tokens, sink, original_error=obs._OMITTED):
        caller = sys._getframe(1)
        target = (caller.f_code is code and sink is trace
                  and any(variable is token_variable for variable, _ in tokens))
        if boundary == "phase":
            target = target and caller.f_locals.get("operation") == "test"
        if target:
            selected.append(True)
            tokens = [(BadReset(), None), *tokens]
        elif sink is trace:
            passed.append(caller.f_code)
        return original(tokens, sink, original_error)
    monkeypatch.setattr(obs, "_reset_tokens", injected)
    with observe(trace) if boundary != "observe" else __import__("contextlib").nullcontext():
        before = (obs._stack.get(), obs._request.get(), obs._meta.get())
        try:
            raise outer_type("outer")
        except outer_type:
            with pytest.raises(RuntimeError) as caught:
                with manager(trace if boundary == "observe" else "test") if boundary != "metadata" else manager(test=1):
                    if body_fails:
                        raise body
            assert caught.value is (body if body_fails else reset_error)
            assert (obs._stack.get(), obs._request.get(), obs._meta.get()) == before
    assert selected == [True] and reached == [True] and trace.failed
    if boundary != "observe":
        assert obs.observe.__wrapped__.__code__ in passed


@pytest.mark.parametrize("outer_type", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("reason", ["batch_bm25", "batch_fts"])
@pytest.mark.parametrize("observer_type", [RuntimeError, asyncio.CancelledError])
def test_selection_dispatch_observer_cancel(outer_type, reason, observer_type):
    from opencrab.ontology.pack_registry import PackInfo, _choose_by_content_with_warnings

    trace = Trace("selection-dispatch")
    emit = trace.emit
    failure = observer_type("observer")
    reached = []
    hybrid = SimpleNamespace(_fts_search_per_pack=lambda *args, **kwargs: {},
                             _bm25_search_per_pack_with_warnings=lambda *args, **kwargs: ({}, []),
                             _fts_search=lambda *args, **kwargs: [],
                             _bm25_search=lambda *args, **kwargs: [])
    def injected(kind, operation, **fields):
        if kind == "decision" and operation == "selection.dispatch" and fields.get("reason") == reason:
            reached.append(reason)
            raise failure
        return emit(kind, operation, **fields)
    trace.emit = injected
    with observe(trace):
        try:
            raise outer_type("outer")
        except outer_type:
            if observer_type is asyncio.CancelledError:
                with pytest.raises(asyncio.CancelledError) as caught:
                    _choose_by_content_with_warnings("question", [PackInfo(pack_id="p1")], hybrid, None)
                assert caught.value is failure
            else:
                result = _choose_by_content_with_warnings("question", [PackInfo(pack_id="p1")], hybrid, None)
                assert result == ([], [], [])
    assert reached == [reason] and trace.failed


@pytest.mark.parametrize("outer_type", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("body_type", [None, RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("observer_type", [RuntimeError, asyncio.CancelledError])
def test_phase_end_observer_failure(outer_type, body_type, observer_type):
    trace = Trace("phase-end")
    emit = trace.emit
    observer = observer_type("observer")
    body = body_type("body") if body_type else None
    reached = []
    def injected(kind, operation, **fields):
        if kind == "decision" and operation == "phase.test":
            reached.append(True)
            raise observer
        return emit(kind, operation, **fields)
    trace.emit = injected
    with observe(trace):
        before = (obs._stack.get(), obs._request.get())
        try:
            raise outer_type("outer")
        except outer_type:
            expected = body or (observer if observer_type is asyncio.CancelledError else None)
            if expected is None:
                with obs.phase("phase.test"):
                    pass
            else:
                with pytest.raises(type(expected)) as caught:
                    with obs.phase("phase.test"):
                        if body is not None:
                            raise body
                assert caught.value is expected
            assert (obs._stack.get(), obs._request.get()) == before
    assert reached == [True] and trace.failed


def test_metadata_reset_site_and_token_restore(monkeypatch):
    trace = Trace("metadata-reset")
    reset = obs._reset_tokens
    failure = RuntimeError("reset")
    reached = []
    passed = []
    class BadReset:
        def reset(self, token):
            reached.append(True)
            raise failure
    def injected(tokens, sink, original=obs._OMITTED):
        caller = sys._getframe(1).f_code
        if sink is trace and caller is obs.metadata.__wrapped__.__code__:
            assert any(variable is obs._meta for variable, _ in tokens)
            tokens = [(BadReset(), None), *tokens]
        else:
            passed.append(caller)
        return reset(tokens, sink, original)
    monkeypatch.setattr(obs, "_reset_tokens", injected)
    with observe(trace):
        before = obs._meta.get()
        with pytest.raises(RuntimeError) as caught:
            with obs.metadata(test=1):
                pass
        assert caught.value is failure and obs._meta.get() == before
    assert reached == [True] and obs.observe.__wrapped__.__code__ in passed and trace.failed


def test_reset_tokens_omitted_exception_compatibility():
    trace = Trace("sentinel")
    failure = RuntimeError("reset")
    outer = RuntimeError("body")
    class BadReset:
        def reset(self, token):
            raise failure
    try:
        raise outer
    except RuntimeError as caught:
        obs._reset_tokens([(BadReset(), None)], trace)
        assert caught is outer
    with pytest.raises(RuntimeError) as caught:
        obs._reset_tokens([(BadReset(), None)], trace)
    assert caught.value is failure and trace.failed


def test_overflow_is_incomplete():
    trace = Trace("synthetic", max_events=1)
    with observe(trace), span("query"):
        pass
    assert "incomplete" in validate(trace.document(), expected_mode="general")


def test_observed_request_connects_query_request_id():
    @observed("query")
    def query():
        return {"text": "synthetic", "metadata": {"value": 1}}

    def request():
        with request_span():
            return query()

    trace = Trace("synthetic")
    plain = request()
    with observe(trace):
        actual = request()
    assert actual == plain
    events = trace.document()["events"]
    root = next(e for e in events if e["operation"] == "request" and e["kind"] == "start")
    children = [e for e in events if e["operation"] == "query"]
    assert all(e["request_id"] == root["span_id"] for e in children)


def test_timer_uses_original_float_values_and_ledger():
    trace = Trace("timer")
    ledger = obs.SampleLedger()
    trace.mode = "ledger"
    with observe(trace), obs.metadata(phase="warm", qid="q", shape="s", repeat=0, attempt=0):
        with span("attempt") as attempt_id, span("bundle"):
            timer = obs.RequestTimer()
            t0 = obs.time.perf_counter()
            timer.start(t0)
            with span("query"):
                pass
            t1 = obs.time.perf_counter()
            timer.end(t1)
            timer.close()
            ledger.append(attempt_id=attempt_id, request_id=timer.ident, t0=t0, t1=t1,
                          elapsed_ms=(t1-t0)*1000, disposition="accepted", status="ok",
                          accepted_for_metric=True, phase="warm", qid="q", shape="s", repeat=0, attempt=0)
    doc = trace.document()
    doc["samples"] = ledger.rows
    assert validate(doc, expected_mode="ledger") == []
    events = [e for e in doc["events"] if e["operation"] == "request"]
    assert events[0]["timer"] == t0
    assert events[1]["timer"] == t1
    assert all(e["request_id"] == timer.ident for e in events)


@pytest.mark.parametrize("damage", ["delete", "duplicate", "parent", "sequence", "time", "request"])
def test_validator_rejects_planted_damage(damage):
    trace = Trace("damage")
    with observe(trace), request_span():
        with span("query"):
            pass
    doc = deepcopy(trace.document())
    if damage == "delete":
        doc["events"].pop(1)
    elif damage == "duplicate":
        doc["events"][1]["span_id"] = doc["events"][0]["span_id"]
    elif damage == "parent":
        doc["events"][1]["parent_id"] = 999
    elif damage == "sequence":
        doc["events"][1]["thread_seq"] = 999
    elif damage == "time":
        doc["events"][1]["monotonic_ns"] = -1
    else:
        doc["events"][1]["request_id"] = 999
    assert validate(doc, expected_mode="general")


def test_threads_restore_context_and_keep_parents_separate():
    trace = Trace("threads")
    barrier = threading.Barrier(2)
    def worker():
        assert obs.active() is None
        with observe(trace), request_span():
            barrier.wait(timeout=5)
            with span("query"):
                pass
        assert obs.active() is None
    thread = threading.Thread(target=worker)
    thread.start()
    with observe(trace), request_span():
        barrier.wait(timeout=5)
        with span("query"):
            pass
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert validate(trace.document(), expected_mode="general") == []
    assert len({e["thread_id"] for e in trace.events}) == 2


@pytest.mark.parametrize("exc", [RuntimeError, asyncio.CancelledError, KeyboardInterrupt])
def test_exception_and_cancel_preserve_original_exception(exc):
    trace = Trace("error")
    with pytest.raises(exc), observe(trace), span("query"):
        raise exc()
    assert trace.events[-1]["status"] in ("cancelled", "error")
    assert validate(trace.document(), expected_mode="general") == []


def test_file_terminal_and_corruption(tmp_path):
    files = obs.TraceFiles(tmp_path / "trace")
    with pytest.raises(ValueError, match="incomplete"):
        obs.read_trace(files.root, expected_mode="general")
    trace = Trace("trace")
    with observe(trace), span("bundle"):
        pass
    assert files.finish(trace, expected_mode="general", cleanup=True, observation={"watchdog": True, "slots": True, "writer": True})
    assert "bundle" in obs.timeline(obs.read_trace(files.root, expected_mode="general", expected_run_id="trace", expected_nonce=files.owner), expected_mode="general")
    with pytest.raises(ValueError):
        obs.read_trace(files.root, expected_mode="general", expected_nonce="wrong")
    with pytest.raises(ValueError):
        obs.read_trace(files.root, expected_mode="general", expected_run_id="other-run")
    path = files.root / "trace.json"
    path.write_text("{}")
    with pytest.raises(ValueError):
        obs.read_trace(files.root, expected_mode="general")


def test_replay_cli_complete_and_corrupt(tmp_path):
    root = Path(__file__).resolve().parents[1]
    files = obs.TraceFiles(tmp_path / "trace")
    trace = Trace(files.run_id, mode="ledger")
    ledger = obs.SampleLedger()
    with observe(trace), obs.metadata(phase="cold", qid="q", shape="s", repeat=0, attempt=0):
        with span("attempt") as attempt_id:
            timer = obs.RequestTimer()
            t0 = obs.time.perf_counter()
            timer.start(t0)
            t1 = obs.time.perf_counter()
            timer.end(t1)
            timer.close()
            ledger.append(attempt_id=attempt_id, request_id=timer.ident, phase="cold", qid="q",
                          shape="s", repeat=0, attempt=0, status="ok", disposition="cold",
                          accepted_for_metric=False, t0=t0, t1=t1, elapsed_ms=(t1-t0)*1000)
    assert files.finish(trace, ledger, expected_mode="ledger", cleanup=True,
                        observation={"watchdog": True, "slots": True, "writer": True})
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(root))
    command = [sys.executable, "-B", str(root / "scripts/qa/search_trace.py"), str(files.root)]
    complete = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert complete.returncode == 0 and "request" in complete.stdout
    (files.root / "trace.json").write_text("{}")
    corrupt = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert corrupt.returncode != 0 and "TRACE: incomplete or invalid" in corrupt.stdout


def test_sink_allocation_failure_does_not_change_result(monkeypatch):
    trace = Trace("failure")
    def broken():
        raise OSError("synthetic")
    monkeypatch.setattr(trace, "allocate", broken)
    with observe(trace), span("query"):
        result = {"text": "synthetic", "metadata": {"nested": [1, 2]}}
    assert result["metadata"]["nested"] == [1, 2]
    assert trace.failed


@pytest.mark.parametrize("damage", ["swap", "duplicate", "delete", "extra", "time", "phase", "classification", "mode"])
def test_ledger_rejects_damage(tmp_path, damage):
    trace = Trace("ledger", mode="ledger")
    ledger = obs.SampleLedger()
    with observe(trace):
        for index in range(2):
            with obs.metadata(phase="warm", qid=str(index), shape="shape", repeat=0, attempt=0):
                with span("attempt") as attempt_id:
                    timer = obs.RequestTimer()
                    t0 = obs.time.perf_counter()
                    timer.start(t0)
                    t1 = obs.time.perf_counter()
                    timer.end(t1)
                    timer.close()
                    ledger.append(attempt_id=attempt_id, request_id=timer.ident, phase="warm",
                                  qid=str(index), shape="shape", repeat=0, attempt=0,
                                  status="ok", disposition="accepted", accepted_for_metric=True,
                                  t0=t0, t1=t1, elapsed_ms=(t1 - t0) * 1000.0)
    doc = deepcopy(trace.document())
    doc["samples"] = deepcopy(ledger.rows)
    assert validate(doc, expected_mode="ledger") == []
    if damage == "swap":
        doc["samples"][0]["request_id"], doc["samples"][1]["request_id"] = (
            doc["samples"][1]["request_id"], doc["samples"][0]["request_id"])
    elif damage == "duplicate":
        doc["samples"].append(deepcopy(doc["samples"][0]))
    elif damage == "delete":
        doc["samples"].pop()
    elif damage == "extra":
        doc["samples"][0]["attempt_id"] = 999
    elif damage == "time":
        doc["samples"][0]["t1"] += 1
    elif damage == "phase":
        doc["samples"][0]["phase"] = "cold"
    elif damage == "classification":
        doc["samples"][0]["accepted_for_metric"] = False
    else:
        doc["mode"] = "general"
    assert validate(doc, expected_mode="ledger")
    with pytest.raises(ValueError):
        obs.timeline(doc, expected_mode="ledger")
    trace.events = doc["events"]
    trace.mode = doc["mode"]
    ledger.rows = doc["samples"]
    files = obs.TraceFiles(tmp_path / "trace")
    assert not files.finish(trace, ledger, expected_mode="ledger", cleanup=True,
                            observation={"watchdog": True, "slots": True, "writer": True})
    with pytest.raises(ValueError):
        obs.read_trace(files.root, expected_mode="ledger")
    from scripts.qa import search_trace as replay
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(sys, "argv", ["search_trace", str(files.root)])
        assert replay.main() == 1


@pytest.mark.parametrize("result,cleanup,finished", [(0, True, False), (0, False, True), (7, True, True)])
def test_trace_cli_failure_result(tmp_path, monkeypatch, result, cleanup, finished):
    from scripts import search_baseline as h

    class Watchdog:
        _state_lock = threading.Lock()
        _ops_lock = threading.Lock()
        state = "STOPPED"
        _ops = {}
        def is_alive(self):
            return not cleanup

    class Writer:
        def is_alive(self):
            return False

    class Files:
        run_id = "test"
        def __init__(self, *args, **kwargs):
            pass
        def finish(self, *args, **kwargs):
            return finished

    def run(args):
        args._lifecycle_owner.update(watchdog=Watchdog(), writer=Writer(),
                                     writer_state={"entered": True, "completed": True},
                                     writer_record_failed=threading.Event())
        return result

    monkeypatch.setattr(h, "cmd_run", run)
    monkeypatch.setattr(h, "check_write_paths", lambda *args, **kwargs: None)
    monkeypatch.setattr(h.trace_obs, "TraceFiles", Files)
    args = ["run", "--corpus", str(tmp_path / "corpus"), "--work", str(tmp_path / "work"),
            "--run-dir", str(tmp_path / "run"), "--out", str(tmp_path / "report"),
            "--trace-out", str(tmp_path / "trace")]
    assert h.main(args) == (result if result else 1)


@pytest.mark.parametrize("failure", [RuntimeError("body"), asyncio.CancelledError("body")])
def test_trace_cli_preserves_exception(tmp_path, monkeypatch, failure):
    from scripts import search_baseline as h

    class Files:
        run_id = "test"
        def __init__(self, *args, **kwargs):
            pass

    def run(args):
        raise failure

    monkeypatch.setattr(h, "cmd_run", run)
    monkeypatch.setattr(h, "check_write_paths", lambda *args, **kwargs: None)
    monkeypatch.setattr(h.trace_obs, "TraceFiles", Files)
    args = ["run", "--corpus", str(tmp_path / "corpus"), "--work", str(tmp_path / "work"),
            "--run-dir", str(tmp_path / "run"), "--out", str(tmp_path / "report"),
            "--trace-out", str(tmp_path / "trace")]
    with pytest.raises(type(failure)) as caught:
        h.main(args)
    assert caught.value is failure


def test_independent_oracle_detects_both_event_edges_bypass():
    calls = []
    @observed("query")
    def query():
        calls.append("query")
        return 7
    trace = Trace("oracle")
    with observe(trace), span("bundle"):
        assert query() == 7
    assert calls.count("query") == sum(e["kind"] == "start" and e["operation"] == "query" for e in trace.events)
    bypass = Trace("bypass")
    calls.clear()
    with observe(bypass), span("bundle"):
        assert query.__wrapped__() == 7
    assert calls.count("query") != sum(e["kind"] == "start" and e["operation"] == "query" for e in bypass.events)


def _generate_corpus(source, corpus):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(source))
    result = subprocess.run([sys.executable, "-B", str(source / "scripts/search_corpus.py"),
                             "generate", "--preset", "S0", "--seed", "542", "--out", str(corpus)],
                            cwd=source, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    timing = json.loads((corpus / "generate-timing.json").read_text())
    assert 0 <= timing["generate_seconds"] < float("inf")
    for name in ("manifest.json", "docs.jsonl", "packs.json", "queries.json"):
        assert (corpus / name).is_file()


def _harness_paths(tmp_path, monkeypatch):
    temp = tmp_path / "temp"
    temp.mkdir()
    paths = [tmp_path / name for name in ("corpus", "work", "run", "report.json", "temp")]
    for index, path in enumerate(paths):
        assert not path.is_symlink()
        for other in paths[index + 1:]:
            assert not path.resolve().is_relative_to(other.resolve())
            assert not other.resolve().is_relative_to(path.resolve())
    monkeypatch.setenv("TMPDIR", str(temp))


def test_actual_harness_active_disabled_s0(tmp_path, monkeypatch):
    from scripts import search_baseline as h
    _harness_paths(tmp_path, monkeypatch)
    corpus = tmp_path / "corpus"
    work = tmp_path / "work"
    _generate_corpus(Path(__file__).resolve().parents[1], corpus)
    assert h.cmd_load(corpus, work) == 0
    reports = []
    for mode in ("disabled", "active"):
        out = tmp_path / (mode + ".json")
        args = SimpleNamespace(corpus=str(corpus), work=str(work),
                               run_dir=str(tmp_path / mode), out=str(out),
                               config="default", repeats=1, fresh_timeout=3.,
                               concurrent_seconds=.2, concurrent_rate=5.)
        if mode == "active":
            trace = Trace("actual", mode="ledger")
            args._sample_ledger = obs.SampleLedger()
            frames = []
            previous = sys.getprofile()
            def profile(frame, event, arg):
                if event == "call" and frame.f_code.co_name == "attempt_scope" and frame not in frames:
                    frames.append(frame)
            try:
                sys.setprofile(profile)
                with observe(trace), span("run"):
                    assert h.cmd_run(args) == 0
            finally:
                sys.setprofile(previous)
            doc = trace.document()
            doc["samples"] = args._sample_ledger.rows
            assert validate(doc, expected_mode="ledger") == []
            assert len(frames) == len(doc["samples"]) == sum(
                e["kind"] == "start" and e["operation"] == "attempt" for e in trace.events
            )
            assert doc["samples"]
            assert {x["disposition"] for x in doc["samples"]} >= {"cold", "accepted", "excluded_warmup"}
            assert all(x["elapsed_ms"] == (x["t1"]-x["t0"])*1000 for x in doc["samples"])
            assert all(x["status"] == "ok" for x in doc["samples"])
            assert all(x["accepted_for_metric"] == (x["disposition"] == "accepted")
                       for x in doc["samples"])
            assert any(e["operation"] == "result_convert" for e in trace.events)
            assert any(e["operation"] == "writer" for e in trace.events)
        else:
            assert h.cmd_run(args) == 0
        reports.append(json.loads(out.read_text()))
    assert [q["legs"] for q in reports[0]["queries"]] == [q["legs"] for q in reports[1]["queries"]]


@pytest.mark.parametrize("stage,exc_type", [
    ("pre_request", RuntimeError),
    ("pre_request", asyncio.CancelledError),
    ("request", RuntimeError),
    ("request", asyncio.CancelledError),
    ("diagnostic", RuntimeError),
    ("diagnostic", asyncio.CancelledError),
    ("consume", RuntimeError),
    ("consume", asyncio.CancelledError),
])
def test_harness_attempt_failure_ledger(tmp_path, monkeypatch, stage, exc_type):
    from scripts import search_baseline as h

    _harness_paths(tmp_path, monkeypatch)
    corpus, work = tmp_path / "corpus", tmp_path / "work"
    _generate_corpus(Path(__file__).resolve().parents[1], corpus)
    assert h.cmd_load(corpus, work) == 0
    args = SimpleNamespace(corpus=str(corpus), work=str(work), run_dir=str(tmp_path / "run"),
                           out=str(tmp_path / "report.json"), config="default", repeats=1,
                           fresh_timeout=3., concurrent_seconds=.2, concurrent_rate=5.)
    args._sample_ledger = obs.SampleLedger()
    args._lifecycle_owner = {}
    trace = Trace("attempt", mode="ledger")
    failure = exc_type("synthetic")

    if stage == "pre_request":
        target, name = h.Watchdog, "op_start"
    elif stage == "request":
        from opencrab.services import pack_selection
        target, name = pack_selection, "resolve_packs"
    elif stage == "diagnostic":
        from opencrab.ontology.query import HybridQuery
        target, name = HybridQuery, "_bm25_search_with_warnings"
    else:
        target, name = h, "judge_bundle"
    original = getattr(target, name)
    calls = []
    owners = []
    metric_before = []
    metric_after = []
    actual_requests = []
    attempt_frames = []
    attempt_keys = []
    previous_profile = sys.getprofile()

    def profile(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "attempt_scope" and frame not in attempt_frames:
            attempt_frames.append(frame)
            values = frame.f_locals
            attempt_keys.append((values["phase"], values["q"]["qid"], values["q"]["shape"],
                                 values["repeat"], values["attempt"]))
        if event == "call" and frame.f_code is h._cmd_run_with_watchdog.__code__:
            owners.append(frame)
        if event == "return" and frame.f_code.co_name == "request" and isinstance(arg, tuple):
            actual_requests.append((frame.f_locals["q"]["qid"], arg[2]))
        if event == "call" and frame.f_code.co_name == "consume" and owners:
            metric_before.append(deepcopy(owners[-1].f_locals["shape_lat"]))

    def injected(*args, **kwargs):
        if stage == "pre_request":
            if len(args) > 1 and str(args[1]).startswith("request:"):
                calls.append(stage)
                raise failure
            return original(*args, **kwargs)
        frame = sys._getframe(1)
        while frame is not None and frame.f_code.co_name != "leg_calls":
            frame = frame.f_back
        if stage != "diagnostic" or frame is not None:
            calls.append(stage)
            if stage == "consume":
                metric_after.append(deepcopy(owners[-1].f_locals["shape_lat"]))
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, injected)
    try:
        sys.setprofile(profile)
        with observe(trace), pytest.raises(exc_type) as captured:
            h.cmd_run(args)
    finally:
        sys.setprofile(previous_profile)
        owners.clear()
    assert captured.value is failure
    owner = args._lifecycle_owner
    assert owner["watchdog"].state == "STOPPED"
    assert not owner["watchdog"].is_alive()
    assert not owner["watchdog"]._ops
    assert "writer" not in owner
    assert calls == [stage]
    row = args._sample_ledger.rows[-1]
    assert row["phase"] == ("cold" if stage in ("request", "pre_request") else "warm")
    assert row["repeat"] == (0 if stage in ("request", "pre_request") else (-1 if stage == "diagnostic" else 0))
    assert row["attempt"] == 0
    if stage == "pre_request":
        assert row["request_id"] is None and all(key not in row for key in ("t0", "t1", "elapsed_ms"))
    else:
        assert row["request_id"] is not None
    assert row["status"] == ("cancelled" if exc_type is asyncio.CancelledError else "error")
    assert row["accepted_for_metric"] is (stage == "consume"), "ledger_oracle_mismatch"
    doc = trace.document()
    doc["samples"] = args._sample_ledger.rows
    assert validate(doc, expected_mode="ledger") == []
    attempt_end = next(event for event in doc["events"]
                       if event.get("span_id") == row["attempt_id"] and event["kind"] == "end")
    assert attempt_end["status"] == row["status"] == row["disposition"]
    if stage == "pre_request":
        assert not any(event["operation"] == "request" and event.get("parent_id") == row["attempt_id"]
                       for event in doc["events"])
    else:
        request_end = next(event for event in doc["events"]
                           if event.get("span_id") == row["request_id"] and event["kind"] == "end")
        assert request_end["status"] == (row["status"] if stage == "request" else "ok")
    assert len({row["attempt_id"] for row in doc["samples"]}) == len(doc["samples"])
    def independently_matches(document):
        keys = ("phase", "qid", "shape", "repeat", "attempt")
        starts = [event for event in document["events"]
                  if event["operation"] == "attempt" and event["kind"] == "start"]
        ends = [event for event in document["events"]
                if event["operation"] == "attempt" and event["kind"] == "end"]
        samples = document["samples"]
        return (sorted(attempt_keys) == sorted(tuple(row[key] for key in keys) for row in samples)
                == sorted(tuple(event[key] for key in keys) for event in starts)
                == sorted(tuple(event[key] for key in keys) for event in ends)
                and {event["span_id"] for event in starts} == {event["span_id"] for event in ends}
                == {row["attempt_id"] for row in samples}
                and len(starts) == len(ends) == len(samples) == len(attempt_keys))
    assert attempt_keys and independently_matches(doc)
    damaged = deepcopy(doc)
    missing_id = damaged["samples"][-1]["attempt_id"]
    damaged["samples"] = [sample for sample in damaged["samples"] if sample["attempt_id"] != missing_id]
    damaged["events"] = [event for event in damaged["events"] if event.get("span_id") != missing_id]
    assert not independently_matches(damaged)
    if stage == "consume":
        before = metric_before[-1].get(row["shape"], [])
        after = metric_after[-1].get(row["shape"], [])
        assert actual_requests[-1] == (row["qid"], row["elapsed_ms"])
        assert after == before + [actual_requests[-1][1]], "metric_oracle_mismatch"


@pytest.mark.parametrize("body_type", [RuntimeError, asyncio.CancelledError, SystemExit])
@pytest.mark.parametrize("clock_type", [RuntimeError, asyncio.CancelledError])
def test_request_error_clock_preserves_identity_and_cleanup(tmp_path, monkeypatch, body_type, clock_type):
    from opencrab.services import pack_selection
    from scripts import search_baseline as h

    _harness_paths(tmp_path, monkeypatch)
    corpus, work = tmp_path / "corpus", tmp_path / "work"
    _generate_corpus(Path(__file__).resolve().parents[1], corpus)
    assert h.cmd_load(corpus, work) == 0
    args = SimpleNamespace(corpus=str(corpus), work=str(work), run_dir=str(tmp_path / "run"),
                           out=str(tmp_path / "report.json"), config="default", repeats=1,
                           fresh_timeout=3., concurrent_seconds=.2, concurrent_rate=5.,
                           _sample_ledger=obs.SampleLedger(), _lifecycle_owner={})
    trace = Trace("clock-failure")
    body = body_type(0 if body_type is SystemExit else "body")
    clock_failure = clock_type("clock")
    original_time = h.time
    armed = False

    class ClockProxy:
        def __getattr__(self, name):
            return getattr(original_time, name)

        def perf_counter(self):
            nonlocal armed
            if armed:
                armed = False
                raise clock_failure
            return original_time.perf_counter()

    def injected(*args, **kwargs):
        nonlocal armed
        armed = True
        raise body

    monkeypatch.setattr(h, "time", ClockProxy())
    monkeypatch.setattr(pack_selection, "resolve_packs", injected)
    with observe(trace):
        before = (obs._stack.get(), obs._request.get(), obs._meta.get())
        with pytest.raises(body_type) as captured:
            h.cmd_run(args)
        assert captured.value is body
        assert (obs._stack.get(), obs._request.get(), obs._meta.get()) == before
    watchdog = args._lifecycle_owner["watchdog"]
    assert not watchdog.is_alive() and not watchdog._ops
    assert watchdog.state == "STOPPED"
    assert trace.failed
    row = args._sample_ledger.rows[-1]
    assert row["accepted_for_metric"] is False
    assert row["status"] == ("error" if body_type is RuntimeError else "cancelled")
    assert "elapsed_ms" not in row and "t1" not in row
    assert not Path(args.out).exists()


@pytest.mark.parametrize("failure_type", [SystemExit, asyncio.CancelledError])
def test_writer_baseexception_rejects_lifecycle(tmp_path, monkeypatch, failure_type):
    from scripts import search_baseline as h

    _harness_paths(tmp_path, monkeypatch)
    corpus, work = tmp_path / "corpus", tmp_path / "work"
    _generate_corpus(Path(__file__).resolve().parents[1], corpus)
    assert h.cmd_load(corpus, work) == 0
    args = SimpleNamespace(corpus=str(corpus), work=str(work), run_dir=str(tmp_path / "run"),
                           out=str(tmp_path / "report.json"), config="default", repeats=1,
                           fresh_timeout=3., concurrent_seconds=.2, concurrent_rate=5.,
                           _lifecycle_owner={})
    original = h.locked_write
    failure = failure_type(0 if failure_type is SystemExit else "writer")
    captured = []
    def injected(*args, **kwargs):
        if threading.current_thread().name == "search-baseline-writer":
            raise failure
        return original(*args, **kwargs)
    def hook(info):
        captured.append(info.exc_value)
    monkeypatch.setattr(h, "locked_write", injected)
    monkeypatch.setattr(threading, "excepthook", hook)
    h.cmd_run(args)
    owner = args._lifecycle_owner
    assert captured == [failure]
    assert owner["writer_state"]["entered"]
    assert not owner["writer_state"]["completed"]
    assert not owner["writer"].is_alive()
    assert not h.lifecycle_observe(owner)
    assert not owner["watchdog"]._ops




def test_writer_record_failure_prevents_success(tmp_path, monkeypatch):
    from scripts import search_baseline as h

    _harness_paths(tmp_path, monkeypatch)
    corpus, work = tmp_path / "corpus", tmp_path / "work"
    _generate_corpus(Path(__file__).resolve().parents[1], corpus)
    assert h.cmd_load(corpus, work) == 0
    class FailingWriterState(dict):
        def __setitem__(self, key, value):
            if key == "completed":
                raise RuntimeError("record")
            return super().__setitem__(key, value)
    args = SimpleNamespace(corpus=str(corpus), work=str(work), run_dir=str(tmp_path / "run"),
                           out=str(tmp_path / "report.json"), config="default", repeats=1,
                           fresh_timeout=3., concurrent_seconds=.2, concurrent_rate=5.,
                           _lifecycle_owner={"writer_state_factory": FailingWriterState})
    h.cmd_run(args)
    owner = args._lifecycle_owner
    assert owner["writer_record_failed"].is_set()
    assert not owner["writer"].is_alive()
    assert not h.lifecycle_observe(owner)


@pytest.mark.parametrize("stage", ["start", "join"])
def test_writer_partial_start_and_join_cancellation(tmp_path, monkeypatch, stage):
    from scripts import search_baseline as h

    _harness_paths(tmp_path, monkeypatch)
    corpus, work = tmp_path / "corpus", tmp_path / "work"
    _generate_corpus(Path(__file__).resolve().parents[1], corpus)
    assert h.cmd_load(corpus, work) == 0
    args = SimpleNamespace(corpus=str(corpus), work=str(work), run_dir=str(tmp_path / "run"),
                           out=str(tmp_path / "report.json"), config="default", repeats=1,
                           fresh_timeout=3., concurrent_seconds=.2, concurrent_rate=5.,
                           _lifecycle_owner={})
    original_start, original_join = threading.Thread.start, threading.Thread.join
    failure = asyncio.CancelledError("writer")
    injected = False
    writer_joins = []
    def start(thread):
        original_start(thread)
        if thread.name == "search-baseline-writer" and stage == "start":
            raise failure
    def join(thread, timeout=None):
        nonlocal injected
        if thread.name == "search-baseline-writer":
            writer_joins.append(timeout)
            if stage == "join" and not injected:
                injected = True
                raise failure
        return original_join(thread, timeout)
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(threading.Thread, "join", join)
    with pytest.raises(asyncio.CancelledError) as captured:
        h.cmd_run(args)
    assert captured.value is failure
    owner = args._lifecycle_owner
    assert not owner["writer"].is_alive()
    assert not owner["watchdog"]._ops
    assert owner["watchdog"].state == "STOPPED"
    assert not owner["watchdog"].is_alive()
    assert owner["writer_execution_failed"].is_set()
    assert len(writer_joins) >= (2 if stage == "join" else 1)
    assert not h.lifecycle_observe(owner)


def test_harness_writer_error_keeps_report_and_joins(tmp_path, monkeypatch):
    from scripts import search_baseline as h

    _harness_paths(tmp_path, monkeypatch)
    corpus, work = tmp_path / "corpus", tmp_path / "work"
    _generate_corpus(Path(__file__).resolve().parents[1], corpus)
    assert h.cmd_load(corpus, work) == 0
    args = SimpleNamespace(corpus=str(corpus), work=str(work), run_dir=str(tmp_path / "run"),
                           out=str(tmp_path / "report.json"), config="default", repeats=1,
                           fresh_timeout=3., concurrent_seconds=.2, concurrent_rate=5.)
    args._sample_ledger = obs.SampleLedger()
    original = h.locked_write
    calls = []

    def injected(*args, **kwargs):
        if threading.current_thread().name == "search-baseline-writer":
            calls.append("writer")
            raise RuntimeError("synthetic")
        return original(*args, **kwargs)

    monkeypatch.setattr(h, "locked_write", injected)
    trace = Trace("writer")
    with observe(trace):
        h.cmd_run(args)
    report = json.loads(Path(args.out).read_text())
    assert calls == ["writer"]
    assert report["concurrent"]["write_errors"] == ["RuntimeError"]
    assert not any(t.name == "search-baseline-writer" for t in threading.enumerate())
    assert any(e["operation"] == "writer" and e["kind"] == "end" for e in trace.events)


@pytest.mark.parametrize("exc_type", [None, RuntimeError, asyncio.CancelledError])
def test_concurrent_metric_oracle(tmp_path, monkeypatch, exc_type):
    from scripts import search_baseline as h

    _harness_paths(tmp_path, monkeypatch)
    corpus, work = tmp_path / "corpus", tmp_path / "work"
    _generate_corpus(Path(__file__).resolve().parents[1], corpus)
    assert h.cmd_load(corpus, work) == 0
    args = SimpleNamespace(corpus=str(corpus), work=str(work), run_dir=str(tmp_path / "run"),
                           out=str(tmp_path / "report.json"), config="default", repeats=1,
                           fresh_timeout=3., concurrent_seconds=2., concurrent_rate=5.)
    args._sample_ledger = obs.SampleLedger()
    original_start = threading.Thread.start
    original_write = h.locked_write
    original_items = h._items_from_results
    started, entered, release = threading.Event(), threading.Event(), threading.Event()
    owner, snapshots, requests, threads = [], [], [], []
    failure = exc_type("synthetic") if exc_type else None
    previous_profile = sys.getprofile()

    def start(thread):
        if thread.name == "search-baseline-writer":
            threads.append(thread)
            started.set()
        return original_start(thread)

    def write(*args, **kwargs):
        if threading.current_thread().name == "search-baseline-writer":
            entered.set()
            if not release.wait(10):
                raise TimeoutError("writer release")
        return original_write(*args, **kwargs)

    def profile(frame, event, arg):
        if event == "call" and frame.f_code is h._cmd_run_with_watchdog.__code__:
            owner.append(frame)
        if started.is_set() and event == "return" and frame.f_code.co_name == "request" and isinstance(arg, tuple):
            assert entered.wait(10)
            requests.append((frame.f_locals["q"]["qid"], frame.f_locals["q"]["shape"], arg[2]))
            snapshots.append(deepcopy(owner[-1].f_locals["conc_lat"]))

    def items(results):
        if started.is_set() and requests:
            try:
                qid, shape, elapsed = requests[-1]
                before = snapshots[-1].get(shape, [])
                after = owner[-1].f_locals["conc_lat"].get(shape, [])
                assert after == before + [elapsed], "metric_oracle_mismatch"
                if failure is not None:
                    raise failure
            finally:
                release.set()
        return original_items(results)

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(h, "locked_write", write)
    monkeypatch.setattr(h, "_items_from_results", items)
    trace = Trace("metric")
    try:
        sys.setprofile(profile)
        with observe(trace):
            if failure is None:
                h.cmd_run(args)
            else:
                with pytest.raises(exc_type) as caught:
                    h.cmd_run(args)
                assert caught.value is failure
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=10)
        sys.setprofile(previous_profile)
        owner.clear()
    assert requests and entered.is_set()
    row = next(row for row in reversed(args._sample_ledger.rows) if row["phase"] == "concurrent")
    assert row["accepted_for_metric"] is True, "ledger_oracle_mismatch"
    assert row["status"] == ("ok" if failure is None else "cancelled" if exc_type is asyncio.CancelledError else "error")






@pytest.mark.parametrize("exc_type", [RuntimeError, asyncio.CancelledError])
def test_harness_concurrent_failure_joins_writer(tmp_path, monkeypatch, exc_type):
    from opencrab.services import pack_selection
    from scripts import search_baseline as h

    _harness_paths(tmp_path, monkeypatch)
    corpus, work = tmp_path / "corpus", tmp_path / "work"
    _generate_corpus(Path(__file__).resolve().parents[1], corpus)
    assert h.cmd_load(corpus, work) == 0
    args = SimpleNamespace(corpus=str(corpus), work=str(work), run_dir=str(tmp_path / "run"),
                           out=str(tmp_path / "report.json"), config="default", repeats=1,
                           fresh_timeout=3., concurrent_seconds=2., concurrent_rate=5.)
    args._sample_ledger = obs.SampleLedger()
    trace = Trace("concurrent")
    failure = exc_type("synthetic")
    original = pack_selection.resolve_packs
    original_start = threading.Thread.start
    original_write = h.locked_write
    started, entered, release = threading.Event(), threading.Event(), threading.Event()
    threads = []

    def start(thread):
        if thread.name == "search-baseline-writer":
            threads.append(thread)
            started.set()
        return original_start(thread)

    def write(*args, **kwargs):
        if threading.current_thread().name == "search-baseline-writer":
            entered.set()
            if not release.wait(10):
                raise TimeoutError("writer release")
        return original_write(*args, **kwargs)

    def injected(*args, **kwargs):
        if started.is_set():
            try:
                assert entered.wait(10)
                raise failure
            finally:
                release.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(h, "locked_write", write)
    monkeypatch.setattr(pack_selection, "resolve_packs", injected)
    try:
        with observe(trace), pytest.raises(exc_type) as captured:
            h.cmd_run(args)
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=10)
    assert captured.value is failure
    assert entered.is_set()
    assert not any(t.name == "search-baseline-writer" for t in threading.enumerate())
    row = args._sample_ledger.rows[-1]
    assert row["phase"] == "concurrent"
    assert row["status"] == ("cancelled" if exc_type is asyncio.CancelledError else "error")
    assert row["accepted_for_metric"] is False
    assert any(e["operation"] == "writer" and e["kind"] == "end" for e in trace.events)
