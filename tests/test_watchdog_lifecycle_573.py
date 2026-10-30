import json
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import search_baseline as h
from scripts.search_baseline import Watchdog, watchdog_scope

from .test_search_baseline import _run


def _validate_s0_fixture(source):
    from scripts.search_corpus import manifest_digest

    corpus, work = source / "corpus", source / "work"
    marker = json.loads((work / "load-complete.json").read_text())
    expected = h.expected_state(corpus)
    observed, problems = h.observed_state(work)
    if marker.get("manifest_digest") != manifest_digest(corpus) or marker.get("expected") != expected:
        raise ValueError("fixture marker does not match corpus")
    if problems or observed != expected:
        raise ValueError("fixture work does not match corpus")
    return source


def test_s0_fixture_rejects_corrupt_marker(tmp_path, s0_fixture):
    import shutil

    shutil.copytree(s0_fixture / "corpus", tmp_path / "corpus")
    shutil.copytree(s0_fixture / "work", tmp_path / "work")
    marker = tmp_path / "work" / "load-complete.json"
    data = json.loads(marker.read_text())
    data["manifest_digest"] = "invalid"
    marker.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="marker does not match"):
        _validate_s0_fixture(tmp_path)


def _configured_s0_fixture(configured):
    if not configured:
        raise ValueError("OPENCRAB_573_FIXTURE is empty")
    return _validate_s0_fixture(Path(configured))


def test_s0_fixture_rejects_empty_and_missing(tmp_path):
    with pytest.raises(ValueError, match="is empty"):
        _configured_s0_fixture("")
    with pytest.raises(FileNotFoundError):
        _configured_s0_fixture(str(tmp_path))


def test_s0_fixture_rejects_corpus_mismatch(tmp_path, s0_fixture):
    import shutil

    shutil.copytree(s0_fixture / "corpus", tmp_path / "corpus")
    shutil.copytree(s0_fixture / "work", tmp_path / "work")
    queries = tmp_path / "corpus" / "queries.json"
    queries.write_text(queries.read_text() + " ")
    with pytest.raises(ValueError):
        _validate_s0_fixture(tmp_path)


@pytest.fixture(scope="module")
def s0_fixture(tmp_path_factory):
    configured = os.environ.get("OPENCRAB_573_FIXTURE")
    if configured is not None:
        return _configured_s0_fixture(configured)
    source = tmp_path_factory.mktemp("search573-fixture")
    corpus, work = source / "corpus", source / "work"
    generated = _run(["generate", "--preset", "S0", "--seed", "542", "--out", str(corpus)],
                     source, "search_corpus.py")
    assert generated.returncode == 0, generated.stdout + generated.stderr
    loaded = _run(["load", "--corpus", str(corpus), "--work", str(work)], source)
    assert loaded.returncode == 0, loaded.stdout + loaded.stderr
    return _validate_s0_fixture(source)


def test_failed_condition_rejects_normal_return(tmp_path):
    watchdog = Watchdog("S0", "run", tmp_path / "abort", [])
    failed = threading.Event()

    def condition():
        failed.set()
        raise RuntimeError("condition")

    watchdog._condition = condition
    with pytest.raises(RuntimeError, match="watchdog cleanup failed"):
        with watchdog_scope(watchdog):
            assert failed.wait(2)
    assert watchdog.state == "FAILED"
    assert not watchdog.is_alive()
    assert not watchdog._ops


def test_failed_condition_preserves_exception_identity(tmp_path):
    watchdog = Watchdog("S0", "run", tmp_path / "abort", [])
    failed = threading.Event()

    def condition():
        failed.set()
        raise RuntimeError("condition")

    watchdog._condition = condition
    error = RuntimeError("body")
    with pytest.raises(RuntimeError) as caught:
        with watchdog_scope(watchdog):
            assert failed.wait(2)
            raise error
    assert caught.value is error
    assert watchdog.state == "FAILED"
    assert not watchdog.is_alive()
    assert not watchdog._ops


def test_parent_nonce_supervision(tmp_path):
    root = Path(__file__).resolve().parents[1]
    run_id = secrets.token_hex(16)
    nonce = secrets.token_hex(16)
    evidence = tmp_path / "lifecycle.json"
    child = subprocess.run(
        [sys.executable, "-I", "-B", str(root / "tests/search_watchdog_child_573.py"),
         str(root), str(evidence), run_id, nonce],
        cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=30,
    )
    assert child.returncode == 0, child.stdout + child.stderr
    observed = json.loads(evidence.read_text())
    assert observed["run_id"] == run_id
    assert observed["nonce"] == nonce
    assert observed["state"] == "STOPPED"
    assert not observed["alive"] and not observed["slots"]
    assert observed["identity"]


@pytest.mark.parametrize("mode", ["normal", "marker-fails", "stderr-write", "stderr-flush", "both"])
def test_timeout_exits_disposable_process(tmp_path, mode):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(root / "tests/search_watchdog_child_573.py"),
         str(root), str(tmp_path / "evidence"), secrets.token_hex(16), secrets.token_hex(16), mode],
        cwd=root, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 3
    if mode in ("normal", "marker-fails"):
        assert "ABORTED test_timeout" in result.stderr
    assert (tmp_path / "abort.json").exists() == (mode not in ("marker-fails", "both"))


def test_lifecycle_channel_uses_parent_nonce(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    path = tmp_path / "evidence" / "lifecycle.json"
    path.parent.mkdir()
    run_id, nonce = secrets.token_hex(16), secrets.token_hex(16)
    args = SimpleNamespace(lifecycle_run_id=run_id, lifecycle_nonce=nonce, lifecycle_path=str(path))
    context = h.lifecycle_prepare(args, [], True)
    watchdog = Watchdog("S0", "run", tmp_path / "abort", [])
    context["owner"]["watchdog"] = watchdog
    with watchdog_scope(watchdog):
        pass
    assert h.lifecycle_publish(context, 0)
    evidence = json.loads(path.read_text())
    assert evidence == {"run_id": run_id, "nonce": nonce, "cleanup": True}
    assert not h.lifecycle_publish(context, 0)
    with pytest.raises(SystemExit):
        h.lifecycle_prepare(args, [], True)


def test_lifecycle_rejects_unsafe_paths(tmp_path, monkeypatch):
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setenv("TMPDIR", str(temp))
    source = tmp_path / "source"
    source.mkdir()
    link = tmp_path / "link"
    link.symlink_to(source, target_is_directory=True)
    for path in (source / "result.json", link / "result.json"):
        args = SimpleNamespace(lifecycle_run_id="run", lifecycle_nonce="nonce", lifecycle_path=str(path))
        with pytest.raises(SystemExit):
            h.lifecycle_prepare(args, [source], True)
    args = SimpleNamespace(lifecycle_run_id="run", lifecycle_nonce=None, lifecycle_path=str(tmp_path / "x"))
    with pytest.raises(SystemExit):
        h.lifecycle_prepare(args, [], True)


def test_lifecycle_write_failure_is_incomplete(tmp_path, monkeypatch):
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setenv("TMPDIR", str(temp))
    path = tmp_path / "result.json"
    args = SimpleNamespace(lifecycle_run_id="run", lifecycle_nonce="nonce", lifecycle_path=str(path))
    context = h.lifecycle_prepare(args, [], True)
    watchdog = Watchdog("S0", "run", tmp_path / "abort", [])
    context["owner"]["watchdog"] = watchdog
    with watchdog_scope(watchdog):
        pass
    def fail_link(*args):
        raise OSError("publish")
    monkeypatch.setattr(h.os, "link", fail_link)
    assert not h.lifecycle_publish(context, 0)
    assert not path.exists()


def test_lifecycle_publication_race(tmp_path, monkeypatch):
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setenv("TMPDIR", str(temp))
    path = tmp_path / "result.json"
    args = SimpleNamespace(lifecycle_run_id="run", lifecycle_nonce="nonce", lifecycle_path=str(path))
    context = h.lifecycle_prepare(args, [], True)
    watchdog = Watchdog("S0", "run", tmp_path / "abort", [])
    context["owner"]["watchdog"] = watchdog
    with watchdog_scope(watchdog):
        pass
    barrier = threading.Barrier(3)
    results = []
    def publish():
        barrier.wait()
        results.append(h.lifecycle_publish(context, 0))
    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=3)
    assert sorted(results) == [False, True]
    assert json.loads(path.read_text())["nonce"] == "nonce"


def test_product_lifecycle_cli(tmp_path):
    root = Path(__file__).resolve().parents[1]
    temp = tmp_path / "temp"
    temp.mkdir()
    env = {**os.environ, "TMPDIR": str(temp), "PYTHONDONTWRITEBYTECODE": "1"}
    import shutil

    def invoke(mode, area, index, script, args, expected=0):
        run_id, nonce = secrets.token_hex(16), secrets.token_hex(16)
        evidence = area / f"lifecycle-{index}.json"
        flags = (["--lifecycle-run-id", run_id, "--lifecycle-nonce", nonce,
                  "--lifecycle-path", str(evidence)] if mode == "supervised" else [])
        result = subprocess.run([sys.executable, "-B", str(root / "scripts" / script), *args, *flags],
                                cwd=root, env=env, capture_output=True, text=True, timeout=120)
        assert result.returncode == expected, result.stdout + result.stderr
        if mode == "supervised":
            assert json.loads(evidence.read_text()) == {"run_id": run_id, "nonce": nonce, "cleanup": True}
        else:
            assert not evidence.exists()

    for mode in ("supervised", "plain"):
        area = tmp_path / mode
        area.mkdir()
        corpus, work = area / "corpus", area / "work"
        invoke(mode, area, 0, "search_corpus.py",
               ["generate", "--preset", "S0", "--seed", "542", "--out", str(corpus)])
        invoke(mode, area, 1, "search_baseline.py", ["load", "--corpus", str(corpus), "--work", str(work)])
    for name in ("manifest.json", "docs.jsonl", "packs.json", "queries.json"):
        assert (tmp_path / "supervised" / "corpus" / name).read_bytes() == (
            tmp_path / "plain" / "corpus" / name).read_bytes()
    assert h.observed_state(tmp_path / "supervised" / "work") == h.observed_state(tmp_path / "plain" / "work")

    def run(mode, area, work, expected=0):
        invoke(mode, area, 2, "search_baseline.py",
               ["run", "--corpus", str(tmp_path / mode / "corpus"), "--work", str(work),
                "--run-dir", str(area / "run"), "--out", str(area / "report.json"),
                "--repeats", "1", "--fresh-timeout", "3",
                "--concurrent-seconds", ".2", "--concurrent-rate", "5"], expected)
        return json.loads((area / "report.json").read_text())

    shared_work = tmp_path / "supervised" / "work"
    reports = [run(mode, tmp_path / mode, shared_work) for mode in ("supervised", "plain")]
    assert reports[0]["verdict"] == reports[1]["verdict"]
    assert reports[0]["queries"] == reports[1]["queries"]

    def default_id(work):
        conn = h._ro(work / "sql.db")
        try:
            return conn.execute("SELECT pack_id FROM packs WHERE owner_id=? AND is_default=1", ("user_a",)).fetchone()[0]
        finally:
            conn.close()

    control = tmp_path / "control"
    control.mkdir()
    other_work = tmp_path / "plain" / "work"
    assert default_id(shared_work) != default_id(other_work)
    other = run("plain", control, other_work)
    def whole(report):
        return next(q for q in report["queries"] if q["shape"] == "whole_scope")
    assert default_id(shared_work) in whole(reports[0])["selection"]["effective"]
    assert default_id(other_work) in whole(other)["selection"]["effective"]
    assert reports[0]["queries"] != other["queries"]

    for field in ("manifest_digest", "expected"):
        area = tmp_path / field
        area.mkdir()
        work = area / "work"
        shutil.copytree(shared_work, work)
        marker = work / "load-complete.json"
        data = json.loads(marker.read_text())
        data[field] = None
        marker.write_text(json.dumps(data))
        rejected = run("plain", area, work, expected=2)
        assert "load-complete.json does not match the corpus" in rejected["reasons"]






@pytest.mark.parametrize("kind", ["delayed", "alive", "alive-query", "join-both", "join-body", "completed-start"])
def test_writer_unresolved_child(tmp_path, kind, s0_fixture):
    import shutil

    root = Path(__file__).resolve().parents[1]
    shutil.copytree(s0_fixture / "corpus", tmp_path / "corpus")
    shutil.copytree(s0_fixture / "work", tmp_path / "work")
    temp = tmp_path / "temp"
    temp.mkdir()
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(root / "tests/search_cli_failure_child_573.py"),
         str(root), "supervised", "writer-protection", kind, str(tmp_path), "run", "nonce"],
        cwd=root, env={**os.environ, "TMPDIR": str(temp), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observation = json.loads(result.stdout.splitlines()[-1])
    assert observation["identity"] and observation["failed"]
    if kind == "completed-start":
        assert observation["completed"] is True and not observation["alive"]
        assert not observation["unresolved"] and not observation["watchdog_alive"]
        assert observation["state"] == "STOPPED"
    else:
        assert observation["unresolved"] and observation["watchdog_alive"]
        assert observation["state"] == "RUNNING"
    assert not observation["success"] and not observation["evidence"]
    assert observation["join_failures"] == (2 if kind in ("join-both", "join-body") else 0)
    assert observation["query_failure_injected"] == (kind == "alive-query")
    assert observation["writer_joined"] and observation["writer_stopped"]
    if kind == "delayed":
        assert observation["ident_none"] and not observation["alive"]
        assert observation["late_entry"]
        assert observation["helper_joined"] and observation["helper_stopped"]
    elif kind != "completed-start":
        assert not observation["ident_none"] and observation["alive"]


@pytest.mark.parametrize("command", ["generate", "load", "run", "fault"])
@pytest.mark.parametrize("mode", ["plain", "supervised"])
@pytest.mark.parametrize("kind", ["error", "cancelled"])
def test_actual_cli_failure_no_success_evidence(tmp_path, command, mode, kind, monkeypatch, s0_fixture):
    import shutil
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(s0_fixture / "corpus", tmp_path / "corpus")
    shutil.copytree(s0_fixture / "work", tmp_path / "work")
    temp = tmp_path / "temp"
    temp.mkdir()
    run_id, nonce = secrets.token_hex(16), secrets.token_hex(16)
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(root / "tests/search_cli_failure_child_573.py"),
         str(root), mode, command, kind, str(tmp_path), run_id, nonce],
        cwd=root, env={**os.environ, "TMPDIR": str(temp), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observation = json.loads(result.stdout.splitlines()[-1])
    assert observation == {"identity": True, "state": "STOPPED", "alive": False, "slots": 0,
                           "evidence": False, "events": ["watchdog_start", "injected_target"],
                           "module_identity": True}
    assert not (tmp_path / "lifecycle.json").exists()


def test_operation_preserves_body_exception(tmp_path, monkeypatch):
    watchdog = Watchdog("S0", "run", tmp_path / "abort", [])
    failure = RuntimeError("body")
    def fail_end(slot="main"):
        raise RuntimeError("cleanup")
    monkeypatch.setattr(watchdog, "op_end", fail_end)
    with pytest.raises(RuntimeError) as caught:
        with watchdog.operation("test"):
            raise failure
    assert caught.value is failure


def test_start_after_launch_preserves_exception(tmp_path, monkeypatch):
    watchdog = Watchdog("S0", "run", tmp_path / "abort", [])
    original = watchdog.start
    failure = RuntimeError("start")
    def partial():
        original()
        raise failure
    monkeypatch.setattr(watchdog, "start", partial)
    with pytest.raises(RuntimeError) as caught:
        with watchdog_scope(watchdog):
            pass
    assert caught.value is failure
    assert not watchdog.is_alive()


def test_join_cancellation_keeps_original_exception(tmp_path, monkeypatch):
    import asyncio
    watchdog = Watchdog("S0", "run", tmp_path / "abort", [])
    join = watchdog.join
    first = True
    def interrupted(timeout=None):
        nonlocal first
        if first:
            first = False
            raise asyncio.CancelledError()
        return join(timeout)
    monkeypatch.setattr(watchdog, "join", interrupted)
    failure = RuntimeError("body")
    with pytest.raises(RuntimeError) as caught:
        with watchdog_scope(watchdog):
            raise failure
    assert caught.value is failure
    assert not watchdog.is_alive()


def test_span_end_failure_preserves_body_and_context(monkeypatch):
    import asyncio

    from opencrab.observability import search_trace as obs
    trace = obs.Trace("span")
    emit = trace.emit
    failure = RuntimeError("body")
    def injected(kind, *args, **kwargs):
        if kind == "end":
            raise asyncio.CancelledError()
        return emit(kind, *args, **kwargs)
    monkeypatch.setattr(trace, "emit", injected)
    with obs.observe(trace):
        before = obs._stack.get()
        with pytest.raises(RuntimeError) as caught:
            with obs.span("test"):
                raise failure
        assert caught.value is failure
        assert obs._stack.get() == before
        assert trace.failed


def test_measured_lock_released_on_wait_record_cancel(monkeypatch):
    import asyncio

    from opencrab.observability import search_trace as obs
    trace = obs.Trace("lock")
    emit = trace.emit
    lock = threading.Lock()
    failure = asyncio.CancelledError()
    def injected(kind, operation, **kwargs):
        if kind == "end" and operation == "write_lock_wait":
            raise failure
        return emit(kind, operation, **kwargs)
    monkeypatch.setattr(trace, "emit", injected)
    with obs.observe(trace), pytest.raises(asyncio.CancelledError) as caught:
        with obs.measured_lock(lock):
            pytest.fail("body must not run")
    assert caught.value is failure
    assert lock.acquire(blocking=False)
    lock.release()
    assert trace.failed


def test_phase_quota_cancel_preserves_body(monkeypatch):
    import asyncio

    from opencrab.observability import search_trace as obs
    trace = obs.Trace("phase")
    count = 0
    def quota():
        nonlocal count
        count += 1
        if count == 2:
            raise asyncio.CancelledError()
        return {}
    monkeypatch.setattr(obs, "quota_snapshot", quota)
    failure = RuntimeError("body")
    with obs.observe(trace), pytest.raises(RuntimeError) as caught:
        with obs.phase("test"):
            raise failure
    assert caught.value is failure
    assert trace.failed


@pytest.mark.parametrize("error_type", [RuntimeError, __import__("asyncio").CancelledError])
def test_decision_failure_is_incomplete(monkeypatch, error_type):
    from opencrab.observability import search_trace as obs
    trace = obs.Trace("decision")
    failure = error_type("observer")
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(trace, "emit", fail)
    with obs.observe(trace):
        if issubclass(error_type, Exception):
            obs.decision("test", "reason")
        else:
            with pytest.raises(error_type) as caught:
                obs.decision("test", "reason")
            assert caught.value is failure
    assert trace.failed


@pytest.mark.parametrize("body_type", [RuntimeError, __import__("asyncio").CancelledError, SystemExit])
@pytest.mark.parametrize("observer_type", [RuntimeError, __import__("asyncio").CancelledError])
def test_span_end_failure_matrix(monkeypatch, body_type, observer_type):
    from opencrab.observability import search_trace as obs
    trace = obs.Trace("matrix")
    original_emit = trace.emit
    body = body_type(0 if body_type is SystemExit else "body")
    def emit(kind, *args, **kwargs):
        if kind == "end":
            raise observer_type("observer")
        return original_emit(kind, *args, **kwargs)
    monkeypatch.setattr(trace, "emit", emit)
    with obs.observe(trace):
        before = (obs._stack.get(), obs._request.get(), obs._meta.get())
        with pytest.raises(body_type) as caught:
            with obs.span("test"):
                raise body
        assert caught.value is body
        assert (obs._stack.get(), obs._request.get(), obs._meta.get()) == before
    assert trace.failed


def test_request_span_restores_context_after_clock_error(monkeypatch):
    from opencrab.observability import search_trace as obs
    trace = obs.Trace("timer")
    failure = RuntimeError("body")
    clock = obs.time.perf_counter
    clock_values = iter((clock(), clock()))
    class Clock:
        def perf_counter(self):
            try:
                return next(clock_values)
            except StopIteration:
                raise RuntimeError("clock") from None
        thread_time_ns = staticmethod(obs.time.thread_time_ns)
    monkeypatch.setattr(obs, "time", Clock())
    with obs.observe(trace):
        stack, request = obs._stack.get(), obs._request.get()
        with pytest.raises(RuntimeError) as caught:
            with obs.request_span():
                raise failure
        assert caught.value is failure
        assert obs._stack.get() == stack and obs._request.get() == request
    assert trace.failed


@pytest.mark.parametrize("observer_type", [RuntimeError, __import__("asyncio").CancelledError])
def test_request_span_emit_failure_preserves_body(monkeypatch, observer_type):
    from opencrab.observability import search_trace as obs

    trace = obs.Trace("request-emit")
    emit = trace.emit
    failure = RuntimeError("body")
    def injected(kind, *args, **kwargs):
        if kind == "end":
            raise observer_type("observer")
        return emit(kind, *args, **kwargs)
    monkeypatch.setattr(trace, "emit", injected)
    with obs.observe(trace):
        before = (obs._stack.get(), obs._request.get())
        with pytest.raises(RuntimeError) as caught:
            with obs.request_span():
                raise failure
        assert caught.value is failure
        assert (obs._stack.get(), obs._request.get()) == before
    assert trace.failed


def test_request_span_disabled_preserves_exception():
    from opencrab.observability import search_trace as obs

    failure = RuntimeError("body")
    before = (obs._stack.get(), obs._request.get())
    with pytest.raises(RuntimeError) as caught:
        with obs.request_span():
            raise failure
    assert caught.value is failure
    assert (obs._stack.get(), obs._request.get()) == before


@pytest.mark.parametrize("operation", ["allocate", "start", "end"])
@pytest.mark.parametrize("error_type", [RuntimeError, __import__("asyncio").CancelledError])
def test_span_observer_failure_modes(monkeypatch, operation, error_type):
    from opencrab.observability import search_trace as obs
    trace = obs.Trace("failure-matrix")
    failure = error_type("observer")
    if operation == "allocate":
        def allocate():
            raise failure
        monkeypatch.setattr(trace, "allocate", allocate)
    else:
        emit = trace.emit
        def injected(kind, *args, **kwargs):
            if kind == operation:
                raise failure
            return emit(kind, *args, **kwargs)
        monkeypatch.setattr(trace, "emit", injected)
    with obs.observe(trace):
        before = (obs._stack.get(), obs._request.get(), obs._meta.get())
        if issubclass(error_type, Exception):
            with obs.span("test"):
                pass
        else:
            with pytest.raises(error_type) as caught:
                with obs.span("test"):
                    pass
            assert caught.value is failure
        assert (obs._stack.get(), obs._request.get(), obs._meta.get()) == before
    assert trace.failed


@pytest.mark.parametrize("error_type", [RuntimeError, __import__("asyncio").CancelledError])
def test_ledger_record_failure_marks_incomplete(error_type):
    from opencrab.observability import search_trace as obs
    ledger = obs.SampleLedger()
    failure = error_type("record")
    class FailingRows(list):
        def append(self, row):
            raise failure
    ledger.rows = FailingRows()
    if issubclass(error_type, Exception):
        ledger.append(status="ok")
    else:
        with pytest.raises(error_type) as caught:
            ledger.append(status="ok")
        assert caught.value is failure
    assert ledger.failed and not ledger.rows


def test_terminal_flush_failure_is_incomplete(tmp_path, monkeypatch):
    from opencrab.observability import search_trace as obs
    trace = obs.Trace("run")
    files = obs.TraceFiles(tmp_path / "trace")
    with obs.observe(trace), obs.span("test"):
        pass
    def failed_fsync(fd):
        raise OSError("fsync")
    monkeypatch.setattr(obs.os, "fsync", failed_fsync)
    assert not files.finish(trace, expected_mode="general", cleanup=True, observation={"watchdog": True, "slots": True, "writer": True})
    assert trace.failed and not (files.root / "terminal.json").exists()
    with pytest.raises(ValueError):
        obs.read_trace(files.root, expected_mode="general")


def test_failed_lifecycle_never_publishes(tmp_path, monkeypatch):
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setenv("TMPDIR", str(temp))
    path = tmp_path / "lifecycle.json"
    args = SimpleNamespace(lifecycle_run_id="run", lifecycle_nonce="nonce", lifecycle_path=str(path))
    context = h.lifecycle_prepare(args, [], True)
    watchdog = Watchdog("S0", "run", tmp_path / "abort", [])
    watchdog.state = "FAILED"
    context["owner"]["watchdog"] = watchdog
    assert not h.lifecycle_publish(context, 0)
    assert not path.exists()


def test_stop_wins_condition_race(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    watchdog = Watchdog("S0", "run", tmp_path / "abort", [])
    def condition():
        entered.set()
        assert release.wait(2)
        return "timeout"
    watchdog._condition = condition
    watchdog.start()
    assert entered.wait(2)
    watchdog.stop()
    release.set()
    watchdog.join(timeout=2)
    assert not watchdog.is_alive()
    assert watchdog.state == "STOPPED"
    assert not watchdog.abort_path.exists()
