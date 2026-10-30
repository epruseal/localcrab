import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

root, mode, command, kind, base, run_id, nonce = sys.argv[1:]
sys.path.insert(0, root)
import scripts.search_baseline as h  # noqa: E402
import scripts.search_corpus as c  # noqa: E402

sys.modules["search_baseline"] = h
import scripts.search_faults as f  # noqa: E402

assert f.sb is h
base = Path(base)
if command == "writer-protection":
    release = threading.Event()
    reached = threading.Event()
    ran = threading.Event()
    failure = asyncio.CancelledError("writer protection")
    original_start = threading.Thread.start
    original_join = threading.Thread.join
    original_ident = threading.Thread._set_ident
    join_failures = []
    join_error = RuntimeError("first join")
    second_join_error = RuntimeError("second join")
    original_alive = threading.Thread.is_alive
    original_locked_write = h.locked_write
    owner = {}
    helper = None
    query_failure_injected = threading.Event()

    def delayed_ident(thread):
        if thread.name == "search-baseline-writer" and kind == "delayed":
            reached.set()
            release.wait(30)
        original_ident(thread)

    def start_writer(thread):
        global helper
        if thread.name != "search-baseline-writer":
            return original_start(thread)
        if kind == "delayed":
            helper = threading.Thread(target=original_start, args=(thread,), daemon=True)
            original_start(helper)
            assert reached.wait(10)
            raise failure
        original_start(thread)
        if kind == "completed-start":
            original_join(thread, 10)
            assert not original_alive(thread) and owner["writer_state"].get("completed") is True
            raise failure
        if kind in ("alive", "alive-query", "join-both", "join-body"):
            assert reached.wait(10)
        if kind == "join-both":
            return
        raise failure

    def join_writer(thread, timeout=None):
        if thread.name == "search-baseline-writer" and kind in ("join-both", "join-body"):
            join_failures.append(True)
            raise join_error if len(join_failures) == 1 else second_join_error
        return original_join(thread, timeout)

    def blocked_write(*args, **kwargs):
        if threading.current_thread().name == "search-baseline-writer":
            reached.set()
            release.wait(30)
        return original_locked_write(*args, **kwargs)

    def alive(thread):
        if thread.name == "search-baseline-writer" and kind == "join-both" and not join_failures:
            return False
        if thread.name == "search-baseline-writer" and kind == "alive-query":
            query_failure_injected.set()
            raise RuntimeError("is_alive")
        return original_alive(thread)

    def writer_entered(key, value):
        if key == "entered" and value is True:
            ran.set()

    class State(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            writer_entered(key, value)

    owner["writer_state_factory"] = State
    threading.Thread._set_ident = delayed_ident
    threading.Thread.start = start_writer
    threading.Thread.join = join_writer
    threading.Thread.is_alive = alive
    if kind not in ("delayed", "completed-start"):
        h.locked_write = blocked_write
    args = SimpleNamespace(corpus=str(base / "corpus"), work=str(base / "work"),
                           run_dir=str(base / "run"), out=str(base / "report.json"),
                           config="default", repeats=1, fresh_timeout=3.,
                           concurrent_seconds=.2, concurrent_rate=5., _lifecycle_owner=owner)
    snapshot = None
    cleanup_error = None
    try:
        try:
            h.cmd_run(args)
        except BaseException as caught:
            identity = caught is (join_error if kind == "join-both" else failure)
        else:
            identity = False
        writer, watchdog = owner["writer"], owner["watchdog"]
        snapshot = {"identity": identity, "ident_none": writer.ident is None,
                    "alive": original_alive(writer), "unresolved": getattr(watchdog, "writer_unresolved", False),
                    "watchdog_alive": watchdog.is_alive(), "state": watchdog.state,
                    "failed": owner["writer_execution_failed"].is_set(),
                    "completed": owner["writer_state"].get("completed"),
                    "query_failure_injected": query_failure_injected.is_set(),
                    "join_failures": len(join_failures)}
        context = {"owner": owner, "path": base / "lifecycle.json", "run_id": run_id, "nonce": nonce}
        threading.Thread.is_alive = original_alive
        snapshot["success"] = h.lifecycle_publish(context, 0)
        snapshot["evidence"] = context["path"].exists()
    finally:
        original_error = sys.exception()
        release.set()
        threading.Thread.is_alive = original_alive
        threading.Thread.join = original_join
        try:
            if helper is not None:
                helper.join(10)
                helper_stopped = not original_alive(helper)
            else:
                helper_stopped = True
            writer = owner.get("writer")
            if writer is None or writer.ident is None:
                raise RuntimeError("writer did not start")
            writer.join(10)
            writer_stopped = not original_alive(writer)
            if not helper_stopped or not writer_stopped:
                raise RuntimeError("writer cleanup failed")
            if snapshot is not None:
                snapshot.update(writer_joined=True, writer_stopped=True)
                if kind == "delayed":
                    snapshot.update(late_entry=ran.is_set(), helper_joined=True, helper_stopped=True)
        except BaseException as exc:
            cleanup_error = exc
        if cleanup_error is not None and original_error is None:
            raise cleanup_error
    print(json.dumps(snapshot), flush=True)
    os._exit(0)

error = RuntimeError("body") if kind == "error" else asyncio.CancelledError("body")
entered = []
events = []
original_start = h.Watchdog.start

def start(watchdog):
    entered.append(watchdog)
    events.append("watchdog_start")
    original_start(watchdog)

def fail(*args, **kwargs):
    events.append("injected_target")
    raise error

h.Watchdog.start = start
corpus, work = base / "corpus", base / "work"
if command == "generate":
    c.generate = fail
    main, argv = c.main, ["generate", "--preset", "S0", "--seed", "542", "--out", str(base / "new-corpus")]
elif command == "load":
    h._open_stores = fail
    main, argv = h.main, ["load", "--corpus", str(corpus), "--work", str(base / "new-work")]
elif command == "run":
    h._cmd_run_with_watchdog = fail
    main, argv = h.main, ["run", "--corpus", str(corpus), "--work", str(work), "--run-dir", str(base / "run"), "--out", str(base / "report.json")]
else:
    f.Ctx = fail
    main, argv = f.main, ["run", "--corpus", str(corpus), "--work", str(work), "--run-root", str(base / "fault"), "--out", str(base / "fault.json"), "--scenarios", "T1"]
evidence = base / "lifecycle.json"
if mode == "supervised":
    argv += ["--lifecycle-run-id", run_id, "--lifecycle-nonce", nonce, "--lifecycle-path", str(evidence)]
try:
    main(argv)
except BaseException as caught:
    identity = caught is error
else:
    identity = False
assert len(entered) == 1
watchdog = entered[0]
print(json.dumps({"identity": identity, "state": watchdog.state, "alive": watchdog.is_alive(),
                  "slots": len(watchdog._ops), "evidence": evidence.exists(), "events": events,
                  "module_identity": f.sb is h}))
