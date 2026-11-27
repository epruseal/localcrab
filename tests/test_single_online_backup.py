"""#718: 단일 SQLite 모드의 무정지 백업 시험.

설계 9판 6절과 7절의 A0, A1, A4, A5가 정본이다. 합성 root와 임시 디렉터리만 쓰고 라이브 경로를 쓰지 않는다.
  A0: 잠금 탐침이 write.lock과 billing.db.lock과 root를 정확히 구별한다.
  A1: 백업이 두 캡처 동안 두 잠금을 연속으로 쥐고 _run_targets 뒤에 푼다.
  A4: crabharness apply가 데이터 쓰기와 이벤트 기록을 한 write.lock 구간에서 한다.
  A5: 쓰기와 crabharness와 질의 부하 아래 만든 세트를 복원하는 왕복 시험.
"""
from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import selectors
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_factory_single_file import (  # noqa: F401  (_fresh_single_registry는 autouse 고정물이다)
    _authorized_and_pointed,
    _plain_table_counts,
    _single_env,
    _single_settings,
)
from tests.test_factory_single_file import (
    _fresh_single_registry as _isolate_single_registry,  # noqa: F401
)
from tests.test_single_backup import _backup

_REPO = Path(__file__).resolve().parent.parent

_PROBE_CODE = (
    "import sys\n"
    "from opencrab.locking import file_lock, write_lock\n"
    "def held(factory):\n"
    "    try:\n"
    "        with factory():\n"
    "            return 0\n"
    "    except TimeoutError:\n"
    "        return 1\n"
    "root = sys.argv[1]\n"
    "w = held(lambda: write_lock(root, timeout=0))\n"
    "b = held(lambda: file_lock('billing.db.lock', root, timeout=0))\n"
    "print(f'W={w} B={b}')\n"
)


def _probe(root: Path) -> dict[str, bool]:
    """하위 프로세스가 두 잠금을 timeout=0으로 시도한다. True는 다른 주체가 쥐고 있다는 뜻이다."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _PROBE_CODE, str(root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    line = _finish(proc, timeout=60).strip().splitlines()[-1]
    fields = dict(part.split("=") for part in line.split())
    return {"write": fields["W"] == "1", "billing": fields["B"] == "1"}


def _reset_billing_events(root: Path) -> None:
    """합성 billing.db의 자리표시 표를 실제 billing_events 표로 바꾼다."""
    from opencrab.billing.hooks import _TABLES_SQLITE

    conn = sqlite3.connect(root / "billing.db")
    try:
        conn.execute("DROP TABLE IF EXISTS billing_events")
        for ddl in _TABLES_SQLITE:
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# A0: 탐침 식별
# ---------------------------------------------------------------------------


def test_a0_probe_tells_the_two_locks_and_the_root_apart(tmp_path):
    from opencrab.locking import file_lock, write_lock

    root = tmp_path / "root"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    assert _probe(root) == {"write": False, "billing": False}
    with write_lock(str(root), timeout=5):
        assert _probe(root) == {"write": True, "billing": False}
        with file_lock("billing.db.lock", str(root), timeout=5):
            assert _probe(root) == {"write": True, "billing": True}
        assert _probe(root) == {"write": True, "billing": False}
    with file_lock("billing.db.lock", str(root), timeout=5):
        assert _probe(root) == {"write": False, "billing": True}
        # 다른 root의 같은 이름 잠금은 이 root의 탐침에 보이지 않는다.
        assert _probe(other) == {"write": False, "billing": False}
    assert _probe(root) == {"write": False, "billing": False}


# ---------------------------------------------------------------------------
# A1: 백업이 두 잠금을 연속으로 쥔다
# ---------------------------------------------------------------------------


def test_a1_backup_holds_both_locks_across_both_captures(tmp_path, monkeypatch):
    """두 캡처(single.db 고정과 billing.db 복사)가 billing.db.lock의 한 보유 구간 안에 있고 _run_targets 뒤에 풀린다."""
    from opencrab import locking
    from opencrab.stores import backup as bk

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    root = built.root
    real_root = os.path.realpath(root)
    log: list[tuple] = []

    real_file_lock = locking.file_lock

    @contextlib.contextmanager
    def recording_file_lock(filename, data_dir=None, **kwargs):
        match = filename == "billing.db.lock" and data_dir is not None and os.path.realpath(data_dir) == real_root
        if match:
            log.append(("enter",))
        try:
            with real_file_lock(filename, data_dir, **kwargs):
                yield
        finally:
            if match:
                log.append(("exit",))

    monkeypatch.setattr(locking, "file_lock", recording_file_lock)

    real_open_snapshot = bk._SnapshotPins.open_snapshot
    real_backup_sqlite = bk.backup_sqlite
    real_run_targets = bk._run_targets
    real_finish_jobs = bk._finish_jobs

    def open_snapshot(self, source):
        if Path(source).name == "single.db":
            log.append(("capture", "single.db", _probe(root)))
        return real_open_snapshot(self, source)

    def backup_sqlite(source, destination, *args, **kwargs):
        log.append(("capture", Path(source).name, _probe(root)))
        return real_backup_sqlite(source, destination, *args, **kwargs)

    def run_targets(*args, **kwargs):
        log.append(("run_targets_start", _probe(root)))
        return real_run_targets(*args, **kwargs)

    def finish_jobs(*args, **kwargs):
        log.append(("finish_jobs", _probe(root)))
        return real_finish_jobs(*args, **kwargs)

    monkeypatch.setattr(bk._SnapshotPins, "open_snapshot", open_snapshot)
    monkeypatch.setattr(bk, "backup_sqlite", backup_sqlite)
    monkeypatch.setattr(bk, "_run_targets", run_targets)
    monkeypatch.setattr(bk, "_finish_jobs", finish_jobs)

    outcome = _backup(built, tmp_path / "bk")

    held_both = {"write": True, "billing": True}
    free_both = {"write": False, "billing": False}
    kinds = [entry[0] for entry in log]
    # billing.db.lock의 진입은 한 번, 그 구간 안에 시작 탐침과 두 캡처가 있고, 퇴출 뒤에 finish_jobs가 온다.
    assert kinds.count("enter") == 1 and kinds.count("exit") == 1, log
    enter, exit_ = kinds.index("enter"), kinds.index("exit")
    inside = log[enter + 1 : exit_]
    assert [e[0] for e in inside] == ["run_targets_start", "capture", "capture"], log
    captures = {e[1]: e[2] for e in inside if e[0] == "capture"}
    assert set(captures) == {"single.db", "billing.db"}, log
    assert captures["single.db"] == held_both and captures["billing.db"] == held_both, log
    start = [e for e in inside if e[0] == "run_targets_start"][0]
    assert start[1] == held_both, log
    finish = [e for e in log if e[0] == "finish_jobs"][0]
    assert log.index(finish) > exit_, log
    assert finish[1] == free_both, log
    assert _probe(root) == free_both

    manifest = json.loads((outcome.set_dir / "MANIFEST.json").read_text())
    by_label = {item["label"]: item for item in manifest["entries"]}
    assert by_label["single.db"]["capture"] == "wal_snapshot"
    assert by_label["billing.db"]["capture"] == "copied_under_lock"
    wait = manifest["lock"].get("billing_lock_wait_seconds")
    assert isinstance(wait, (int, float)) and not isinstance(wait, bool) and wait >= 0, manifest["lock"]


def _await_held(proc: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    data = b""
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise AssertionError(f"holder signal timed out: {data!r}")
            chunk = os.read(proc.stdout.fileno(), 64)
            if not chunk:
                raise AssertionError(f"holder closed before signal: {data!r}")
            data += chunk
    assert data == b"held\n", data


def test_a2_billing_lock_timeout_names_the_lock_and_leaves_nothing(tmp_path, monkeypatch):
    """다른 프로세스가 billing.db.lock을 쥐면 백업은 잠금 이름을 담은 TimeoutError로 끝나고 세트도 잠금도 남기지 않는다."""
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    holder_code = (
        "import sys, time\n"
        "from opencrab.locking import file_lock\n"
        "with file_lock('billing.db.lock', sys.argv[1], timeout=5):\n"
        "    print('held', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(built.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        _await_held(holder, timeout=30)
        with pytest.raises(TimeoutError) as failure:
            _backup(built, tmp_path / "bk", lock_timeout=0.5)
        message = str(failure.value)
        assert "billing.db.lock" in message
        assert re.search(r"waited [0-9]+(?:\.[0-9]+)?s", message)
        assert "run this again" in message
        assert "A billing event write holds it" not in message
    finally:
        try:
            holder.stdin.write("done\n")
            holder.stdin.flush()
        finally:
            _finish(holder, timeout=30)
    assert [p.name for p in (tmp_path / "bk").iterdir()] == []
    assert _probe(built.root) == {"write": False, "billing": False}


# ---------------------------------------------------------------------------
# A4: crabharness apply가 데이터와 이벤트를 한 write.lock 구간에서 쓴다
# ---------------------------------------------------------------------------


@pytest.fixture
def _crabharness_on_path():
    """저장소의 crabharness 패키지를 import 가능하게 하고 끝나면 되돌린다."""
    removed = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "crabharness" or name.startswith("crabharness.")
    }
    old_path = list(sys.path)
    sys.path.insert(0, str(_REPO / "crabharness"))
    try:
        yield
    finally:
        sys.path[:] = old_path
        for name in list(sys.modules):
            if name == "crabharness" or name.startswith("crabharness."):
                del sys.modules[name]
        sys.modules.update(removed)


def test_a4_crabharness_apply_writes_data_and_event_in_one_write_lock_hold(
    tmp_path, monkeypatch, _crabharness_on_path
):
    from opencrab import locking
    from opencrab.auth import Principal, principal_scope
    from opencrab.billing import hooks
    from opencrab.ontology import builder as builder_mod

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    _reset_billing_events(built.root)
    root = built.root
    log: list[tuple] = []
    depth = {"n": 0}

    real_write_lock = locking.write_lock

    @contextlib.contextmanager
    def recording_write_lock(*args, **kwargs):
        with real_write_lock(*args, **kwargs):
            depth["n"] += 1
            if depth["n"] == 1:
                log.append(("enter",))
            try:
                yield
            finally:
                if depth["n"] == 1:
                    log.append(("exit",))
                depth["n"] -= 1

    monkeypatch.setattr(locking, "write_lock", recording_write_lock)

    real_add_node = builder_mod.OntologyBuilder.add_node
    real_on_apply = hooks.BillingHooks.on_harness_apply

    def add_node(self, *args, **kwargs):
        if kwargs.get("node_id") == "a4-node":
            log.append(("data",))
        return real_add_node(self, *args, **kwargs)

    def on_harness_apply(self, *args, **kwargs):
        log.append(("event", "REFUSED" if _probe(root)["write"] else "FREE"))
        return real_on_apply(self, *args, **kwargs)

    monkeypatch.setattr(builder_mod.OntologyBuilder, "add_node", add_node)
    monkeypatch.setattr(hooks.BillingHooks, "on_harness_apply", on_harness_apply)

    package = {
        "package_id": "a4-pkg",
        "mission_id": "a4-mission",
        "run_id": "a4-run",
        "nodes": [{"space": "subject", "node_type": "Team", "node_id": "a4-node", "properties": {"name": "a4"}}],
        "edges": [],
    }
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package), encoding="utf-8")

    from crabharness.apply import apply_promotion_package

    with principal_scope(Principal(user_id=built.owner, is_local=True, disabled=False)):
        result = apply_promotion_package(str(path))

    assert result["errors"] == [], result
    assert result["billing"]["ok"] is True, result["billing"]
    assert result["billing"]["billed_node_count"] == 1
    assert log == [("enter",), ("data",), ("event", "REFUSED"), ("exit",)], log


# ---------------------------------------------------------------------------
# A5: 부하 아래 백업과 복원 왕복
# ---------------------------------------------------------------------------

_WORKER_CODE = r'''
import hashlib, json, os, sys, time

root, kind, stopfile, repo, user_id, pack_id = sys.argv[1:7]
worker_timeout = float(sys.argv[7]) if len(sys.argv) > 7 else 240.0
DIM = 4
os.environ["LOCAL_DATA_DIR"] = root
os.environ["STORAGE_MODE"] = "local"
os.environ["SINGLE_SQLITE_FILE"] = os.path.join(root, "single.db")
os.environ["EMBED_DIM"] = str(DIM)
os.environ["EMBEDDING_BACKEND"] = "openai"
sys.path.insert(0, os.path.join(repo, "crabharness"))


def hash_embed(texts):
    out = []
    for text in texts:
        digest = hashlib.sha256(str(text).encode()).digest()
        out.append([digest[i] / 255.0 for i in range(DIM)])
    return out


from opencrab.auth import Principal, principal_scope
from opencrab.mcp.tools import dispatch_tool
from opencrab.stores import factory

factory._make_kure_embedding_function = lambda settings: hash_embed
deadline = time.monotonic() + worker_timeout


def should_run():
    if os.path.exists(stopfile):
        return False
    if time.monotonic() >= deadline:
        print("load deadline expired", file=sys.stderr)
        sys.exit(4)
    return True


def add_node(node_id, name):
    result = dispatch_tool(
        "ontology_add_node",
        {"space": "subject", "node_type": "Team", "node_id": node_id, "properties": {"name": name}},
    )
    if isinstance(result, dict) and result.get("error"):
        print("add_node error", result, file=sys.stderr)
        sys.exit(3)


def query(question, limit):
    result = dispatch_tool("ontology_query", {"question": question, "limit": limit, "pack_ids": [pack_id]})
    if isinstance(result, dict) and result.get("error"):
        print("query error", result, file=sys.stderr)
        sys.exit(3)
    return result


with principal_scope(Principal(user_id=user_id, is_local=True, disabled=False)):
    if kind == "prep":
        add_node("prep-1", "zebra prepnode quasar")
    elif kind == "lookup":
        result = query("zebra prepnode quasar", 10)
        ids = [item.get("node_id") for item in result.get("results", [])]
        node = dispatch_tool("ontology_get_node", {"node_id": "prep-1"})
        print(json.dumps({"total": result.get("total"), "ids": ids, "node": node}))
    elif kind == "marker":
        add_node("marker-node", "marker node")
        query("marker-question", 1)
    elif kind == "write":
        i = 0
        while should_run():
            i += 1
            add_node(f"wl-{os.getpid()}-{i}", f"team {i}")
            time.sleep(0.002)
    elif kind == "query":
        i = 0
        while should_run():
            i += 1
            query(f"probe {i}", 1)
            time.sleep(0.005)
    elif kind == "crab":
        import tempfile
        from crabharness.apply import apply_promotion_package

        i = 0
        while should_run():
            i += 1
            package = {
                "package_id": f"pk-{os.getpid()}-{i}",
                "mission_id": "m",
                "run_id": "r",
                "nodes": [{"space": "subject", "node_type": "Team", "node_id": f"cr-{os.getpid()}-{i}",
                           "properties": {"name": f"crab {i}"}}],
                "edges": [],
            }
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump(package, fh)
            result = apply_promotion_package(fh.name)
            os.unlink(fh.name)
            if result.get("errors") or not result["billing"]["ok"]:
                print("apply error", result, file=sys.stderr)
                sys.exit(3)
'''


def _spawn(root: Path, kind: str, stop: Path, user_id: str, pack_id: str, timeout: float = 240) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _WORKER_CODE, str(root), kind, str(stop), str(_REPO), user_id, pack_id, str(timeout)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(proc: subprocess.Popen, timeout: float = 180) -> str:
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException as exc:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=30)
        except BaseException as cleanup_exc:
            raise BaseExceptionGroup("child communication and cleanup failed", [exc, cleanup_exc]) from None
        raise
    assert proc.returncode == 0, err[-1500:] if err else f"child exited with {proc.returncode}"
    return out


def _run_once(root: Path, kind: str, user_id: str, pack_id: str) -> str:
    return _finish(_spawn(root, kind, root / "never-created-stop", user_id, pack_id))


def _read(path: Path, sql: str, *, live: bool = False):
    """세트 파일은 immutable로 읽는다. 쓰는 중인 root의 파일은 WAL을 보도록 일반 읽기 전용으로 연다."""
    suffix = "?mode=ro" if live else "?mode=ro&immutable=1"
    conn = sqlite3.connect(f"{path.resolve().as_uri()}{suffix}", uri=True, timeout=30)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _state(single: Path, billing: Path, *, live: bool = False) -> dict[str, int]:
    """접두사별 노드 수와 이벤트 합과 질의 이벤트 수. 빈 집합의 합은 COALESCE로 0이다."""
    event_sum = "SELECT COALESCE(SUM(count), 0) FROM billing_events WHERE event_type = '%s'"
    return {
        "wl_nodes": _read(single, "SELECT COUNT(*) FROM graph_nodes WHERE node_id LIKE 'wl-%'", live=live),
        "cr_nodes": _read(single, "SELECT COUNT(*) FROM graph_nodes WHERE node_id LIKE 'cr-%'", live=live),
        "node_write": _read(billing, event_sum % "node_write", live=live),
        "harness_apply": _read(billing, event_sum % "harness_apply", live=live),
        "queries": _read(billing, "SELECT COUNT(*) FROM billing_events WHERE event_type = 'query'", live=live),
    }


def _wait_for(predicate, what: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def test_a5_backup_under_load_restores_to_one_commit_point(tmp_path, monkeypatch):
    from opencrab.stores import single_restore as sr

    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    root = built.root
    single, billing = root / "single.db", root / "billing.db"
    _reset_billing_events(root)
    user_id = str(built.owner)

    pack_id = str(built.pack_id)
    _run_once(root, "prep", user_id, pack_id)
    base = _state(single, billing, live=True)
    base_nw = base["node_write"]
    lookup = json.loads(_run_once(root, "lookup", user_id, pack_id).strip().splitlines()[-1])
    _assert_lookup(lookup, pack_id)

    stop = tmp_path / "stop"
    procs: list[subprocess.Popen] = []
    sets = []
    try:
        for kind in ("write", "crab", "query"):
            procs.append(_spawn(root, kind, stop, user_id, pack_id))
        for _ in range(2):
            _wait_for(
                lambda: _all_running(procs)
                and (not sets or _grown(_state(single, billing, live=True), sets[-1]["state"])),
                "load progress",
            )
            outcome = _backup(built, tmp_path / "bk")
            state = _state(outcome.set_dir / "single.db", outcome.set_dir / "billing.db")
            sets.append({"dir": outcome.set_dir, "state": state})
    finally:
        failures: list[BaseException] = []
        try:
            stop.write_text("stop")
        except BaseException as exc:
            failures.append(exc)
        for proc in procs:
            try:
                _finish(proc)
            except BaseException as exc:
                failures.append(exc)
        if failures:
            active = sys.exception()
            if active is not None:
                failures.insert(0, active)
            raise BaseExceptionGroup("load execution or cleanup failed", failures)

    first, second = sets[0]["state"], sets[1]["state"]
    assert _grown(second, first), (first, second)
    for state in (first, second):
        # 데이터 쓰기와 이벤트가 한 write.lock 구간이므로 세트 안에서 서로 어긋나지 않는다.
        assert state["node_write"] - base_nw == state["wl_nodes"], (state, base_nw)
        assert state["harness_apply"] == state["cr_nodes"], state

    _run_once(root, "marker", user_id, pack_id)
    marker_node = "SELECT COUNT(*) FROM graph_nodes WHERE node_id = 'marker-node'"
    marker_row = "SELECT COUNT(*) FROM billing_events WHERE metadata LIKE '%marker-question%'"
    set_single, set_billing = sets[1]["dir"] / "single.db", sets[1]["dir"] / "billing.db"
    assert _read(single, marker_node, live=True) == 1 and _read(billing, marker_row, live=True) == 1
    assert _read(set_single, marker_node) == 0 and _read(set_billing, marker_row) == 0

    rc = sr.restore_single(sets[1]["dir"], _single_settings(root), apply=True, on_event=lambda line: None)
    assert rc == sr.RC_OK, rc
    assert _read(single, marker_node, live=True) == 0 and _read(billing, marker_row, live=True) == 0
    assert _plain_table_counts(single) == _plain_table_counts(set_single)
    assert _plain_table_counts(billing) == _plain_table_counts(set_billing)
    assert _state(single, billing, live=True) == second

    after = json.loads(_run_once(root, "lookup", user_id, pack_id).strip().splitlines()[-1])
    _assert_lookup(after, pack_id)
    status = subprocess.Popen(
        [sys.executable, "-m", "opencrab.cli", "status"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "LOCAL_DATA_DIR": str(root),
            "STORAGE_MODE": "local",
            "SINGLE_SQLITE_FILE": str(single),
            "EMBED_DIM": "4",
            "EMBEDDING_BACKEND": "openai",
            "VECTOR_BACKEND": "sqlite-vec",
            "COLUMNS": "300",
            "NO_COLOR": "1",
        },
    )
    _assert_status(_finish(status, timeout=120))


def _assert_lookup(result: dict, pack_id: str) -> None:
    assert "prep-1" in result["ids"], result
    direct = result["node"]
    assert direct.get("found") is True, direct
    assert direct["node"]["id"] == "prep-1", direct
    assert direct["node"]["pack_id"] == pack_id, direct


def _assert_status(output: str) -> None:
    labels = ("Graph (SQLite)", "Vector (sqlite-vec)", "Docs (JSON files)", "SQL (SQLite)", "Billing (SQLite)")
    for label in labels:
        rows = [[cell.strip() for cell in line.split("│")] for line in output.splitlines()]
        matches = [cells for cells in rows if len(cells) == 5 and cells[1] == label]
        assert len(matches) == 1, (label, matches, output)
        assert matches[0][3] == "OK", (label, matches)


def test_lookup_and_status_reject_failure_controls():
    good = {"ids": ["prep-1"], "node": {"found": True, "node": {"id": "prep-1", "pack_id": "pack"}}}
    _assert_lookup(good, "pack")
    with pytest.raises(AssertionError):
        _assert_lookup({**good, "node": {"found": False}}, "pack")
    with pytest.raises(AssertionError):
        _assert_lookup(good, "other-pack")
    labels = ("Graph (SQLite)", "Vector (sqlite-vec)", "Docs (JSON files)", "SQL (SQLite)", "Billing (SQLite)")
    output = "\n".join(f"│ {label} │ path │ OK │" for label in labels)
    _assert_status(output)
    with pytest.raises(AssertionError):
        _assert_status(output.replace("OK │", "UNAVAILABLE │", 1))
    with pytest.raises(AssertionError):
        _assert_status(output + "\n" + output.splitlines()[0])
    with pytest.raises(AssertionError):
        _assert_status(output.replace("Billing (SQLite)", "Old Billing (SQLite)"))
    with pytest.raises(AssertionError):
        _assert_status("\n".join(output.splitlines()[:-1]))


@pytest.mark.parametrize("kind", ["write", "query", "crab"])
def test_load_worker_exit_conditions(tmp_path, monkeypatch, kind):
    built = _authorized_and_pointed(tmp_path, monkeypatch)
    _single_env(monkeypatch, built.root)
    _reset_billing_events(built.root)
    stop = tmp_path / "stop"
    proc = _spawn(built.root, kind, stop, str(built.owner), str(built.pack_id), timeout=0)
    with pytest.raises(AssertionError, match="load deadline expired"):
        _finish(proc)
    assert proc.poll() is not None
    stop.write_text("stop")
    proc = _spawn(built.root, kind, stop, str(built.owner), str(built.pack_id), timeout=60)
    _finish(proc)
    assert proc.returncode == 0


def test_worker_stop_and_deadline_order():
    module = ast.parse(_WORKER_CODE)
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "should_run")
    namespace = {"os": os, "sys": sys, "time": time, "stopfile": "unused", "deadline": 1.0}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<worker>", "exec"), namespace)
    should_run = namespace["should_run"]
    calls = []

    class PathProbe:
        @staticmethod
        def exists(path):
            calls.append(path)
            return False

    class Clock:
        @staticmethod
        def monotonic():
            # The stop file appears after its first observation.
            namespace["os"] = type("LateStop", (), {"path": type("Path", (), {"exists": lambda path: True})})
            return 2.0

    namespace["os"] = type("NoStop", (), {"path": PathProbe})
    namespace["time"] = Clock
    with pytest.raises(SystemExit) as failure:
        should_run()
    assert failure.value.code == 4 and calls == ["unused"]
    assert should_run() is False
    loops = [node for node in ast.walk(module) if isinstance(node, ast.While)]
    assert len(loops) == 3
    assert all(isinstance(loop.test, ast.Call) and getattr(loop.test.func, "id", None) == "should_run" for loop in loops)


def test_holder_partial_signal_times_out_and_child_is_reaped():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys,time; sys.stdout.write('held'); sys.stdout.flush(); time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        with pytest.raises(AssertionError, match="timed out"):
            _await_held(proc, timeout=0.2)
    finally:
        with pytest.raises(subprocess.TimeoutExpired):
            _finish(proc, timeout=0.2)
    assert proc.poll() is not None


def _all_running(procs: list[subprocess.Popen]) -> bool:
    """부하 자식이 끝났거나 죽었으면 기다리지 않고 실패시킨다."""
    for proc in procs:
        if proc.poll() is not None:
            raise AssertionError(f"load child exited early: rc={proc.returncode}")
    return True


def _grown(now: dict[str, int], before: dict[str, int]) -> bool:
    keys = ("wl_nodes", "cr_nodes", "node_write", "harness_apply", "queries")
    return all(now[key] > before[key] for key in keys)
