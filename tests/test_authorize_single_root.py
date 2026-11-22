"""병합 도구의 authorize 하위 명령(#671 B0)의 시험.

ready 게시 root의 대장을 authorized로 한 번 올리는 전이를 본다. 시험 ID는 설계 9절의 ID를 따른다.
이름 끝의 mvp 표시는 MVP 시험이다. 표시가 없는 시험은 MLP 시험이다.
기준 입력은 합성 원본으로 병합이 만든 ready root다. 거부 시험은 모두 사유 전체 문자열과 root 바이트 불변을 함께 단언한다.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_factory_single_file import (  # noqa: F401 (autouse 등록부 격리 fixture를 이 파일에도 건다)
    _authorized_root,
    _fresh_single_registry,
    _make_four,
    _point_at,
    _single_settings,
    _write_ledger_state,
)
from tests.test_merge_sqlite_stores import (
    SCRIPT,
    _ready_setup,
    _ReadyHarness,
    merge_mod,
    source_dir,  # noqa: F401 (fixture)
)

_PRE = "authorize preflight failed: "
_WRITE = "authorize write failed: "
_NAMES = ("billing.db", "ledger.json", "manifest.json", "marker.json", "single.db", "single.db.lifetime.lock")
_REPO = SCRIPT.parent.parent
_APPLIED = re.compile(
    r"^authorize applied: kind=(\S+) root=(.+) evidence=(\S+) run_id=(\S+) ledger_sha256=([0-9a-f]{64})$", re.M)


# ---- 도우미 ----


def _make_ready(src, tmp_path, capsys, name="published"):
    """합성 원본에서 병합으로 ready root를 만들고 증거 파일 둘을 곁에 둔다."""
    ctx = _ready_setup(src, tmp_path)
    if name != "published":
        root = tmp_path / name
        ctx.args[ctx.args.index("--publication-root") + 1] = str(root)
        ctx.root = root
        ctx.staging = tmp_path / (name + ".staging")
        ctx.intent = tmp_path / (name + ".intent.json")
        ctx.intent_tmp = tmp_path / (name + ".intent.json.tmp")
        ctx.lock_root = tmp_path / (name + ".lifetime.lock")
    assert merge_mod.main(ctx.args) == 0, capsys.readouterr().err
    capsys.readouterr()
    assert json.loads((ctx.root / "ledger.json").read_text())["state"] == "ready"
    assert json.loads(ctx.intent.read_text())["state"] == "ready"
    assert sorted(os.listdir(ctx.root)) == sorted(_NAMES)
    ctx.verify_pass = tmp_path / "verify-pass.txt"
    ctx.verify_pass.write_text("PASS verification\n")
    ctx.copy_pass = tmp_path / "copy-pass.txt"
    ctx.copy_pass.write_text("PASS copy test\n")
    return ctx


@pytest.fixture
def ready(source_dir, tmp_path, capsys):  # noqa: F811
    return _make_ready(source_dir, tmp_path, capsys)


def _cmd(ctx, kind="copy-test", *, apply=True, run_id="run-1", verify=None, copy=None, root=None):
    args = ["authorize", "--root", str(root if root is not None else ctx.root), "--run-kind", kind,
            "--verification-pass", str(verify if verify is not None else ctx.verify_pass)]
    if kind == "copy-test":
        args += ["--copy-test-run-id", run_id]
    else:
        args += ["--copy-test-pass", str(copy if copy is not None else ctx.copy_pass)]
    if apply:
        args.append("--apply")
    return args


def _run(args, capsys):
    rc = merge_mod.main(args)
    return rc, capsys.readouterr().err


def _sha(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _entry(path: Path):
    info = os.lstat(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
    return (info.st_mode, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, digest)


def _snapshot(ctx, skip=()):
    """root 안 항목, intent, 바깥 잠금 파일, 부모 이름 집합의 상태. 바이트 불변 단언에 쓴다."""
    snap = {}
    for item in sorted(ctx.root.iterdir()):
        if item.name not in skip:
            snap["root/" + item.name] = _entry(item)
    for item in (ctx.intent, ctx.lock_root):
        if os.path.lexists(item) and item.name not in skip:
            snap[item.name] = _entry(item)
    snap["parent"] = tuple(sorted(p.name for p in ctx.root.parent.iterdir() if p.name not in skip))
    return snap


def _ledger(ctx) -> dict:
    return json.loads((ctx.root / "ledger.json").read_text())


def _authorized_bytes(ledger: dict) -> bytes:
    return json.dumps({**ledger, "state": "authorized"}, sort_keys=True, separators=(",", ":")).encode()


def _edit_json(path: Path, **changes) -> None:
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")))


def _edit_single_db(root: Path, *statements: str) -> None:
    conn = sqlite3.connect(root / "single.db")
    conn.isolation_level = None
    try:
        for statement in statements:
            conn.execute(statement)
    finally:
        conn.close()
    assert sorted(os.listdir(root)) == sorted(_NAMES)


def _lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line]


def _assert_rejected(rc, err, reason, *, tail=False):
    expected = _PRE + reason
    lines = _lines(err)
    assert rc == 2, err
    assert (any(line.startswith(expected) for line in lines) if tail else expected in lines), err
    assert "authorize applied" not in err and "dry-run ok" not in err, err


def _assert_write_failed(rc, err, reason, observed):
    lines = [line for line in _lines(err) if line.startswith(_WRITE)]
    assert rc == 1, err
    assert len(lines) == 1, err
    assert lines[0].startswith(_WRITE + reason), err
    assert lines[0].endswith("; observed ledger state: " + observed), err
    assert "authorize applied" not in err, err


def _reject_case(ctx, capsys, reason, *, apply=True, tail=False, skip=(), kind="copy-test"):
    """현재 디스크 상태에서 authorize를 돌려 거부와 root 바이트 불변을 단언한다."""
    before = _snapshot(ctx, skip=skip)
    rc, err = _run(_cmd(ctx, kind, apply=apply), capsys)
    _assert_rejected(rc, err, reason, tail=tail)
    assert _snapshot(ctx, skip=skip) == before


_BOTH_MODES = pytest.mark.parametrize("apply", [True, False], ids=["apply", "check"])


# ---- 정상 BT-N ----


def test_bt_n1_copy_test_apply_mvp(ready, capsys):
    """BT-N1 [MVP] copy-test 적용."""
    ctx = ready
    old = _ledger(ctx)
    before = _snapshot(ctx, skip=("ledger.json",))
    rc, err = _run(_cmd(ctx), capsys)
    assert rc == 0, err
    match = _APPLIED.search(err)
    assert match, err
    kind, root, evidence, run_id, ledger_sha = match.groups()
    assert (kind, root, run_id) == ("copy-test", str(ctx.root), "run-1")
    path, digest = evidence.rsplit(":", 1)
    assert (path, digest) == (str(ctx.verify_pass), _sha(ctx.verify_pass))
    assert (ctx.root / "ledger.json").read_bytes() == _authorized_bytes(old)
    assert ledger_sha == _sha(ctx.root / "ledger.json")
    assert _ledger(ctx) == {**old, "state": "authorized"}
    assert _snapshot(ctx, skip=("ledger.json",)) == before
    assert sorted(os.listdir(ctx.root)) == sorted(_NAMES)
    assert json.loads(ctx.intent.read_text())["state"] == "ready"


def test_bt_n2_production_apply_mvp(ready, capsys):
    """BT-N2 [MVP] production 적용. 두 증거의 경로와 sha256이 출력에 남는다."""
    ctx = ready
    old = _ledger(ctx)
    before = _snapshot(ctx, skip=("ledger.json",))
    rc, err = _run(_cmd(ctx, "production"), capsys)
    assert rc == 0, err
    match = _APPLIED.search(err)
    assert match, err
    kind, _root, evidence, run_id, ledger_sha = match.groups()
    assert (kind, run_id) == ("production", "none")
    pairs = [item.rsplit(":", 1) for item in evidence.split(",")]
    assert pairs == [[str(ctx.verify_pass), _sha(ctx.verify_pass)], [str(ctx.copy_pass), _sha(ctx.copy_pass)]]
    assert (ctx.root / "ledger.json").read_bytes() == _authorized_bytes(old)
    assert ledger_sha == _sha(ctx.root / "ledger.json")
    assert _snapshot(ctx, skip=("ledger.json",)) == before
    assert json.loads(ctx.intent.read_text())["state"] == "ready"


def test_bt_n3_factory_roundtrip_mvp(tmp_path, monkeypatch, capsys, _fresh_single_registry):  # noqa: F811
    """BT-N3 [MVP] 왕복. authorize 전에는 factory가 거부하고 authorize 뒤에는 root를 연다."""
    from opencrab.stores import factory

    built = _authorized_root(tmp_path, monkeypatch)
    capsys.readouterr()
    root = built.root
    _point_at(monkeypatch, root)
    _write_ledger_state(root, "ready")
    settings = _single_settings(root)
    with pytest.raises(RuntimeError, match="single SQLite generation is not authorized"):
        factory.make_sql_store(settings)
    (tmp_path / "verify-pass.txt").write_text("PASS verification\n")
    ctx = SimpleNamespace(root=root, verify_pass=tmp_path / "verify-pass.txt")
    rc, err = _run(_cmd(ctx), capsys)
    assert rc == 0, err
    stores = _make_four(settings)
    try:
        assert stores[3].shared_file is not None
        assert stores[0].shared_file is stores[3].shared_file
    finally:
        for store in reversed(stores):
            store.close()


def test_bt_n4_check_mode_changes_nothing_mvp(ready, capsys):
    """BT-N4 [MVP] 점검만. 바이트와 정체성이 불변이고 이어서 적용이 성공한다."""
    ctx = ready
    before = _snapshot(ctx)
    rc, err = _run(_cmd(ctx, apply=False), capsys)
    assert rc == 0, err
    assert "authorize dry-run ok" in err and "authorize applied" not in err, err
    assert _snapshot(ctx) == before
    rc, err = _run(_cmd(ctx), capsys)
    assert rc == 0, err
    assert _ledger(ctx)["state"] == "authorized"


def test_bt_n5_lock_released_mvp(ready, capsys):
    """BT-N5 [MVP] 성공 뒤 바깥 잠금을 이 프로세스가 배타 비차단으로 잡는다."""
    rc, err = _run(_cmd(ready), capsys)
    assert rc == 0, err
    fd = os.open(ready.lock_root, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def _ordered(events, wanted_first, wanted_last):
    """wanted_first 목록은 첫 발생 위치, wanted_last 목록은 마지막 발생 위치로 순서를 모은다."""
    positions = []
    for event in wanted_first:
        assert event in events, (event, events)
        positions.append(events.index(event))
    for event in wanted_last:
        assert event in events, (event, events)
        positions.append(len(events) - 1 - events[::-1].index(event))
    return positions


def test_bt_n6_event_order_mvp(ready, monkeypatch, capsys):
    """BT-N6 [MVP] 사건 순서. fsync와 rename 기록기는 이 시험이 설치한다."""
    harness = _ReadyHarness(monkeypatch, ready)
    events = harness.events
    real_fsync, real_rename = os.fsync, os.rename

    def fsync(fd):
        events.append(("fsync", harness.by_fd.get(fd)))
        return real_fsync(fd)

    def rename(src, dst, *args, **kwargs):
        events.append(("rename", str(dst)))
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "rename", rename)
    rc, err = _run(_cmd(ready), capsys)
    assert rc == 0, err
    positions = _ordered(
        events,
        [("open", "root-lock"), ("open", "root-file"), ("open", "ledger-tmp"), ("fsync", "ledger-tmp"),
         ("rename", "ledger.json"), ("fsync", "root-dir")],
        [("open", "root-file")])
    assert positions == sorted(positions) and len(set(positions)) == len(positions), (positions, events)


# ---- 오류 BT-E ----


@_BOTH_MODES
@pytest.mark.parametrize("state", ["staged", "Ready", "", 7], ids=["staged", "Ready", "empty", "number"])
def test_bt_e1_ledger_state_not_ready_mvp(ready, capsys, state, apply):
    """BT-E1과 E1b [MVP] ledger state가 ready가 아니다."""
    _edit_json(ready.root / "ledger.json", state=state)
    _reject_case(ready, capsys, "ledger state differs", apply=apply)


def test_bt_e2_ledger_already_authorized_mvp(ready, capsys):
    """BT-E2 [MVP] ledger state가 authorized다."""
    _edit_json(ready.root / "ledger.json", state="authorized")
    _reject_case(ready, capsys, "ledger state differs")


@_BOTH_MODES
@pytest.mark.parametrize("state", ["preparing", "publishing"])
def test_bt_e3_intent_state_not_ready_mvp(ready, capsys, state, apply):
    """BT-E3 [MVP] intent state가 ready가 아니다."""
    _edit_json(ready.intent, state=state)
    _reject_case(ready, capsys, "intent state differs", apply=apply)


_DUPLICATE_INTENT = ('{"database":"x","database":"y","db_uuid":"a","generation":"b","manifest_digest":"c",'
                     '"root":"r","state":"ready"}')


@pytest.mark.parametrize("variant", ["missing", "broken", "duplicate", "list", "symlink"])
def test_bt_e4_intent_unreadable_mvp(ready, tmp_path, capsys, variant):
    """BT-E4 [MVP] intent 파일이 없거나 깨졌거나 심볼릭 링크다."""
    ctx = ready
    if variant == "missing":
        ctx.intent.unlink()
        reason = "intent is missing"
    else:
        reason = "intent is invalid"
        if variant == "broken":
            ctx.intent.write_text("{")
        elif variant == "duplicate":
            ctx.intent.write_text(_DUPLICATE_INTENT)
        elif variant == "list":
            ctx.intent.write_text("[]")
        else:
            real = tmp_path / "intent-real.json"
            os.replace(ctx.intent, real)
            ctx.intent.symlink_to(real)
    _reject_case(ctx, capsys, reason)


@pytest.mark.parametrize("variant", ["extra", "short", "no-state"])
def test_bt_e5_intent_key_set_mvp(ready, capsys, variant):
    """BT-E5 [MVP] intent 키 집합 불일치. state 키가 없어도 추적 없이 키 집합 사유로 거부한다."""
    data = json.loads(ready.intent.read_text())
    if variant == "extra":
        data["extra"] = "x"
    elif variant == "short":
        data.pop("generation")
    else:
        data.pop("state")
    ready.intent.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")))
    before = _snapshot(ready)
    rc, err = _run(_cmd(ready), capsys)
    _assert_rejected(rc, err, "intent key set differs")
    assert "Traceback" not in err and "KeyError" not in err, err
    assert _snapshot(ready) == before


def test_bt_e5b_ledger_extra_key_mvp(ready, capsys):
    """BT-E5b [MVP] ledger에 여분 키가 있다."""
    _edit_json(ready.root / "ledger.json", extra="x")
    _reject_case(ready, capsys, "ledger key set differs")


@pytest.mark.parametrize("key", ["root", "database", "db_uuid", "generation", "manifest_digest"])
def test_bt_e6_ledger_intent_mismatch_mvp(ready, capsys, key):
    """BT-E6 [MVP] intent 파일의 식별 키 하나를 바꾼다. ledger를 바꾸면 결속 검증이 먼저 걸린다."""
    _edit_json(ready.intent, **{key: "changed"})
    _reject_case(ready, capsys, "ledger differs from intent")


@pytest.mark.parametrize("variant", ["manifest", "marker", "metadata", "ledger-root"])
def test_bt_e7_binding_mismatch_mvp(ready, capsys, variant):
    """BT-E7 [MVP] 결속 불일치 네 변형. 사유에 validate_single_file_binding의 원문이 든다."""
    ctx = ready
    if variant == "manifest":
        (ctx.root / "manifest.json").write_bytes((ctx.root / "manifest.json").read_bytes() + b"\n")
        text = "single SQLite manifest binding differs"
    elif variant == "marker":
        _edit_json(ctx.root / "marker.json", generation="0" * 32)
        text = "single SQLite generation binding differs"
    elif variant == "metadata":
        _edit_single_db(ctx.root, "UPDATE single_file_metadata SET db_uuid = '" + "0" * 32 + "'")
        text = "single SQLite generation binding differs"
    else:
        _edit_json(ctx.root / "ledger.json", root="/elsewhere/published")
        text = "single SQLite path binding differs"
    _reject_case(ctx, capsys, "binding differs: " + text)


@pytest.mark.parametrize("content, text", [(b"{", "binding JSON is invalid"), (b"[]", "binding JSON is not an object")],
                         ids=["broken", "list"])
@pytest.mark.parametrize("name", ["ledger.json", "manifest.json", "marker.json"])
def test_bt_e7c_binding_file_invalid(ready, capsys, name, content, text):
    """BT-E7c [MLP] 결속 파일 하나가 JSON이 아니거나 객체가 아니다. 사유는 파일 형태 거부로 시작한다."""
    (ready.root / name).write_bytes(content)
    _reject_case(ready, capsys, "binding file is invalid: " + text)


@pytest.mark.parametrize("rows", [0, 2])
def test_bt_e7b_metadata_row_count_mvp(ready, capsys, rows):
    """BT-E7b [MVP] single_file_metadata 행이 0개나 2개다."""
    if rows == 0:
        _edit_single_db(ready.root, "DELETE FROM single_file_metadata")
    else:
        _edit_single_db(
            ready.root,
            "ALTER TABLE single_file_metadata RENAME TO old_meta",
            "CREATE TABLE single_file_metadata (root TEXT, database_path TEXT, db_uuid TEXT, generation TEXT)",
            "INSERT INTO single_file_metadata SELECT root, database_path, db_uuid, generation FROM old_meta",
            "INSERT INTO single_file_metadata SELECT root, database_path, db_uuid, generation FROM old_meta",
            "DROP TABLE old_meta")
    _reject_case(ready, capsys, "metadata row count differs")


@_BOTH_MODES
def test_bt_e8_quick_check_bad_mvp(ready, monkeypatch, capsys, apply):
    """BT-E8 [MVP] quick_check 불량. 점검만 변형도 같은 거부다."""
    harness = _ReadyHarness(monkeypatch, ready)
    harness.inject_connect("published/single.db?mode=ro&immutable=1", bad_quick_check=True)
    try:
        _reject_case(ready, capsys, "quick_check differs", apply=apply)
    finally:
        harness.discard()


@_BOTH_MODES
@pytest.mark.parametrize("variant", ["tmp", "wal"])
def test_bt_e9_leftover_names(ready, capsys, variant, apply):
    """BT-E9 [MLP] 점검 시작 전부터 임시 파일이나 곁파일이 있다."""
    (ready.root / ("ledger.json.tmp" if variant == "tmp" else "single.db-wal")).write_bytes(b"")
    _reject_case(ready, capsys, "root file set differs", apply=apply)


@pytest.mark.parametrize("variant", ["staging", "intent-tmp"])
def test_bt_e10_parent_artifacts(ready, capsys, variant):
    """BT-E10 [MLP] 부모에 .staging이나 .intent.json.tmp가 남아 있다."""
    if variant == "staging":
        ready.staging.mkdir()
    else:
        ready.intent_tmp.write_bytes(b"x")
    _reject_case(ready, capsys, "root artifacts remain")


def test_bt_e11_sidecar_created_during_check(ready, monkeypatch, capsys):
    """BT-E11 [MLP] 점검 도중 곁파일이 생긴다. 감싸기는 ready root를 만든 뒤에 설치한다."""
    real = merge_mod._connect
    root = ready.root

    def connect(path, *args, **kwargs):
        conn = real(path, *args, **kwargs)
        if Path(path) == root / "single.db":
            (root / "single.db-wal").write_bytes(b"")
        return conn

    monkeypatch.setattr(merge_mod, "_connect", connect)
    _reject_case(ready, capsys, "root database sidecar remains", skip=("single.db-wal",))


@pytest.mark.parametrize("variant", ["ledger-link", "single-hardlink", "extra-file", "missing-file"])
def test_bt_e12_entry_form(ready, tmp_path, capsys, variant):
    """BT-E12 [MLP] 항목 형태. 링크와 여분 파일과 빠진 파일."""
    ctx = ready
    if variant == "ledger-link":
        real = tmp_path / "ledger-real.json"
        os.replace(ctx.root / "ledger.json", real)
        (ctx.root / "ledger.json").symlink_to(real)
        reason = "root file form differs"
    elif variant == "single-hardlink":
        os.link(ctx.root / "single.db", tmp_path / "single-link.db")
        reason = "root file form differs"
    elif variant == "extra-file":
        (ctx.root / "extra.txt").write_bytes(b"x")
        reason = "root file set differs"
    else:
        (ctx.root / "marker.json").unlink()
        reason = "root file set differs"
    _reject_case(ctx, capsys, reason)


def _run_cli(args, timeout=60):
    env = {**os.environ, "PYTHONPATH": str(_REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=str(_REPO), check=False)
    except subprocess.TimeoutExpired:
        pytest.fail("authorize did not finish while the root lock was held by another process")


@pytest.mark.parametrize("mode", [fcntl.LOCK_EX, fcntl.LOCK_SH], ids=["exclusive", "shared"])
def test_bt_e13ab_lock_held_by_other_process(ready, mode):
    """BT-E13a와 E13b [MLP] 다른 프로세스가 바깥 잠금을 배타나 공유로 쥔다. 하위 프로세스가 제한 시간 안에 rc 2로 끝난다."""
    before = _snapshot(ready)
    fd = os.open(ready.lock_root, os.O_RDWR)
    try:
        fcntl.flock(fd, mode)
        result = _run_cli(_cmd(ready))
    finally:
        os.close(fd)
    assert result.returncode == 2, result.stderr
    assert _PRE + "root lifetime lock is busy" in _lines(result.stderr), result.stderr
    assert _snapshot(ready) == before


def test_bt_e13c_lock_file_replaced_while_opening(ready, monkeypatch, capsys):
    """BT-E13c [MLP] 틀 안의 열기 사이에 잠금 파일이 교체된다."""
    harness = _ReadyHarness(monkeypatch, ready)
    real = merge_mod._PublicationOwner.open_fd
    lock = ready.lock_root

    def wrapped(owner, role, *args, **kwargs):
        fd = real(owner, role, *args, **kwargs)
        if role == "root-lock":
            swap = lock.with_name("swap.lock")
            swap.write_bytes(b"")
            os.replace(swap, lock)
        return fd

    monkeypatch.setattr(merge_mod._PublicationOwner, "open_fd", wrapped)
    try:
        _reject_case(ready, capsys, "root lifetime lock identity differs", skip=(lock.name,))
    finally:
        harness.discard()


def test_bt_e15_lock_file_missing(ready, capsys):
    """BT-E15 [MLP] 바깥 잠금 파일이 없다. 거부 뒤에도 새로 생기지 않는다."""
    ready.lock_root.unlink()
    _reject_case(ready, capsys, "root lifetime lock is missing")
    assert not os.path.lexists(ready.lock_root)


def test_bt_e15b_lock_file_replaced_before_acquire(ready, monkeypatch, capsys):
    """BT-E15b [MLP] 사전 확인과 획득 사이에 잠금 파일이 새 inode로 교체된다."""
    real = merge_mod._PublicationOwner.acquire_root_lock
    lock = ready.lock_root
    swapped = {}

    def wrapped(owner, root, **kwargs):
        swap = lock.with_name("swap.lock")
        swap.write_bytes(b"")
        os.replace(swap, lock)
        swapped["ino"] = os.lstat(lock).st_ino
        return real(owner, root, **kwargs)

    monkeypatch.setattr(merge_mod._PublicationOwner, "acquire_root_lock", wrapped)
    rc, err = _run(_cmd(ready), capsys)
    _assert_rejected(rc, err, "root lifetime lock identity differs")
    assert os.lstat(lock).st_ino == swapped["ino"]
    assert _ledger(ready)["state"] == "ready"


def _evidence_variant(ctx, tmp_path, variant):
    """(종류, verification 경로, copy 경로, 사유)를 돌려준다."""
    if variant == "v1-missing":
        return "copy-test", tmp_path / "nothing.txt", None, "evidence path is missing"
    if variant == "v2-symlink":
        link = tmp_path / "pass-link.txt"
        link.symlink_to(ctx.verify_pass)
        return "copy-test", link, None, "evidence path is a symlink"
    if variant == "v3-directory":
        directory = tmp_path / "pass-dir"
        directory.mkdir()
        return "copy-test", directory, None, "evidence path is not a regular file"
    if variant == "v3-fifo":
        fifo = tmp_path / "pass-fifo"
        os.mkfifo(fifo)
        return "copy-test", fifo, None, "evidence path is not a regular file"
    if variant == "v4-empty":
        empty = tmp_path / "pass-empty.txt"
        empty.write_bytes(b"")
        return "copy-test", empty, None, "evidence file is empty"
    if variant == "v5-unreadable":
        if os.geteuid() == 0:
            pytest.skip("root reads every file")
        hidden = tmp_path / "pass-hidden.txt"
        hidden.write_text("PASS\n")
        hidden.chmod(0)
        return "copy-test", hidden, None, "evidence path is not readable"
    if variant == "v6-same-path":
        return "production", ctx.verify_pass, ctx.verify_pass, "evidence files are not distinct"
    if variant == "v6-hardlink":
        twin = tmp_path / "pass-twin.txt"
        os.link(ctx.verify_pass, twin)
        return "production", ctx.verify_pass, twin, "evidence files are not distinct"
    return "copy-test", ctx.root / "manifest.json", None, "evidence path is inside root"


@pytest.mark.parametrize("variant", ["v1-missing", "v2-symlink", "v3-directory", "v3-fifo", "v4-empty",
                                     "v5-unreadable", "v6-same-path", "v6-hardlink", "v7-inside-root"])
def test_bt_e16_evidence_paths(ready, tmp_path, capsys, variant):
    """BT-E16 [MLP] 증거 경로 일곱 변형."""
    kind, verify, copy, reason = _evidence_variant(ready, tmp_path, variant)
    before = _snapshot(ready)
    rc, err = _run(_cmd(ready, kind, verify=verify, copy=copy), capsys)
    _assert_rejected(rc, err, reason)
    assert _snapshot(ready) == before


_USAGE = "usage: merge_sqlite_stores.py authorize"


def _usage_args(ctx, variant):
    base = _cmd(ctx)
    prod = _cmd(ctx, "production")

    def without(args, flag):
        index = args.index(flag)
        return args[:index] + args[index + 2:]

    def replaced(args, flag, value):
        args = list(args)
        args[args.index(flag) + 1] = value
        return args

    if variant == "u1-root":
        return without(base, "--root"), "the following arguments are required: --root"
    if variant == "u1-kind":
        return without(base, "--run-kind"), "the following arguments are required: --run-kind"
    if variant == "u1-verification":
        return without(base, "--verification-pass"), "the following arguments are required: --verification-pass"
    if variant == "u2-missing":
        return without(base, "--copy-test-run-id"), "--copy-test-run-id is required"
    if variant.startswith("u2-bad"):
        value = {"u2-bad-empty": "", "u2-bad-long": "a" * 65, "u2-bad-space": "a b", "u2-bad-slash": "a/b",
                 "u2-bad-korean": "한글"}[variant]
        return replaced(base, "--copy-test-run-id", value), "--copy-test-run-id is invalid"
    if variant == "u3":
        return [*base, "--copy-test-pass", str(ctx.copy_pass)], "--copy-test-pass is not allowed for run kind copy-test"
    if variant == "u4":
        return without(prod, "--copy-test-pass"), "--copy-test-pass is required for run kind production"
    if variant == "u5":
        return [*prod, "--copy-test-run-id", "run-1"], "--copy-test-run-id is not allowed for run kind production"
    return replaced(base, "--run-kind", "bogus"), "invalid choice"


@pytest.mark.parametrize("variant", ["u1-root", "u1-kind", "u1-verification", "u2-missing", "u2-bad-empty",
                                     "u2-bad-long", "u2-bad-space", "u2-bad-slash", "u2-bad-korean", "u3", "u4",
                                     "u5", "u6"])
def test_bt_e17_usage_errors(ready, capsys, variant):
    """BT-E17 [MLP] 인자 여섯 변형. usage 줄과 사유로 구현 전 오류와 구별한다."""
    args, reason = _usage_args(ready, variant)
    before = _snapshot(ready)
    with pytest.raises(SystemExit) as info:
        merge_mod.main(args)
    err = capsys.readouterr().err
    assert info.value.code == 2
    assert err.startswith(_USAGE), err
    assert reason in err, err
    assert _snapshot(ready) == before


@pytest.mark.parametrize("variant", ["relative", "link-root", "link-parent", "dotdot", "slash", "missing", "file"])
def test_bt_e18_root_paths(ready, tmp_path, capsys, variant):
    """BT-E18 [MLP] root 경로 다섯 변형."""
    ctx = ready
    if variant == "relative":
        root, reason = "published", "root path is not absolute"
    elif variant == "link-root":
        link = tmp_path / "root-link"
        link.symlink_to(ctx.root)
        root, reason = link, "root path is not canonical"
    elif variant == "link-parent":
        link = tmp_path.parent / (tmp_path.name + "-parent-link")
        link.symlink_to(tmp_path)
        root, reason = link / "published", "root path is not canonical"
    elif variant == "dotdot":
        root, reason = str(ctx.root) + "/..", "root name is invalid"
    elif variant == "slash":
        root, reason = "/", "root name is invalid"
    elif variant == "missing":
        root, reason = tmp_path / "nothing", "root directory is missing"
    else:
        plain = tmp_path / "plain-file"
        plain.write_bytes(b"x")
        root, reason = plain, "root path is not a directory"
    before = _snapshot(ctx)
    try:
        rc, err = _run(_cmd(ctx, root=root), capsys)
    finally:
        if variant == "link-parent":
            link.unlink()
    _assert_rejected(rc, err, reason)
    assert _snapshot(ctx) == before


def test_bt_e18b_root_replaced_during_check(ready, tmp_path, monkeypatch, capsys):
    """BT-E18b [MLP] root 디렉터리를 점검 중에 교체한다."""
    harness = _ReadyHarness(monkeypatch, ready)
    real = merge_mod._PublicationOwner.open_fd
    moved = tmp_path / "published.orig"
    done = []

    def wrapped(owner, role, *args, **kwargs):
        fd = real(owner, role, *args, **kwargs)
        if role == "root-dir" and not done:
            done.append(True)
            os.rename(ready.root, moved)
            os.mkdir(ready.root)
        return fd

    old_ledger = (ready.root / "ledger.json").read_bytes()
    monkeypatch.setattr(merge_mod._PublicationOwner, "open_fd", wrapped)
    try:
        rc, err = _run(_cmd(ready), capsys)
    finally:
        harness.discard()
    _assert_rejected(rc, err, "root directory differs")
    assert sorted(os.listdir(moved)) == sorted(_NAMES)
    assert (moved / "ledger.json").read_bytes() == old_ledger
    assert os.listdir(ready.root) == []


# ---- 쓰기 단계 BT-E19 ----


def _harness_for(monkeypatch, ctx):
    return _ReadyHarness(monkeypatch, ctx)


def _after_preflight(monkeypatch, action):
    """점검과 쓰기 사이에 action을 한 번 실행한다."""
    real = getattr(merge_mod, "_authorize_preflight", None)

    def wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        action()
        return result

    monkeypatch.setattr(merge_mod, "_authorize_preflight", wrapped, raising=False)


def test_bt_e19a_tmp_open_fails_mvp(ready, monkeypatch, capsys):
    """BT-E19a [MVP] 임시 파일 열기가 실패한다."""
    harness = _harness_for(monkeypatch, ready)
    harness.inject_open("ledger-tmp")
    old = (ready.root / "ledger.json").read_bytes()
    try:
        rc, err = _run(_cmd(ready), capsys)
        _assert_write_failed(rc, err, "ledger tmp open failed:", "ready")
        assert (ready.root / "ledger.json").read_bytes() == old
        assert not (ready.root / "ledger.json.tmp").exists()
    finally:
        harness.discard()


def test_bt_e19b_tmp_appears_before_write(ready, monkeypatch, capsys):
    """BT-E19b [MLP] 점검과 쓰기 사이에 임시 파일이 생긴다. O_EXCL이 걸리고 그 파일은 그대로다."""
    harness = _harness_for(monkeypatch, ready)
    old = (ready.root / "ledger.json").read_bytes()
    _after_preflight(monkeypatch, lambda: (ready.root / "ledger.json.tmp").write_bytes(b"keep"))
    try:
        rc, err = _run(_cmd(ready), capsys)
        _assert_write_failed(rc, err, "ledger tmp open failed:", "ready")
        assert (ready.root / "ledger.json.tmp").read_bytes() == b"keep"
        assert (ready.root / "ledger.json").read_bytes() == old
    finally:
        harness.discard()


@pytest.mark.parametrize("variant", ["write", "fsync"])
def test_bt_e19c_tmp_write_fails(ready, monkeypatch, capsys, variant):
    """BT-E19c [MLP] 임시 파일 쓰기나 fsync가 실패한다."""
    harness = _harness_for(monkeypatch, ready)
    old = _ledger(ready)
    old_bytes = (ready.root / "ledger.json").read_bytes()
    if variant == "write":
        real_write = os.write
        calls = {"n": 0}

        def write(fd, data):
            if fd == harness.slot_fd("ledger-tmp", 0):
                calls["n"] += 1
                if calls["n"] == 1:
                    return real_write(fd, bytes(data)[:3])
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", write)
    else:
        harness.inject_fd_call("fsync", "ledger-tmp")
    try:
        rc, err = _run(_cmd(ready), capsys)
        _assert_write_failed(rc, err, "ledger tmp write failed:", "ready")
        tmp = ready.root / "ledger.json.tmp"
        assert tmp.exists()
        expected = _authorized_bytes(old)
        assert tmp.read_bytes() == (expected[:3] if variant == "write" else expected)
        assert (ready.root / "ledger.json").read_bytes() == old_bytes
    finally:
        harness.discard()


def test_bt_e19d_rename_not_performed_mvp(ready, monkeypatch, capsys):
    """BT-E19d [MVP] rename이 실패하고 임시 파일이 남는다."""
    harness = _harness_for(monkeypatch, ready)
    harness.inject_rename("ledger.json", 0)
    old = _ledger(ready)
    old_bytes = (ready.root / "ledger.json").read_bytes()
    rc, err = _run(_cmd(ready), capsys)
    _assert_write_failed(rc, err, "ledger rename failed: not performed", "ready")
    assert (ready.root / "ledger.json").read_bytes() == old_bytes
    assert (ready.root / "ledger.json.tmp").read_bytes() == _authorized_bytes(old)


def test_bt_e19e_rename_took_effect_mvp(ready, monkeypatch, capsys):
    """BT-E19e [MVP] rename 뒤 오류가 보고된다."""
    harness = _harness_for(monkeypatch, ready)
    harness.inject_rename("ledger.json", 0, after=True)
    old = _ledger(ready)
    rc, err = _run(_cmd(ready), capsys)
    _assert_write_failed(rc, err, "ledger rename failed: took effect", "authorized")
    assert (ready.root / "ledger.json").read_bytes() == _authorized_bytes(old)
    assert not (ready.root / "ledger.json.tmp").exists()


def test_bt_e19f_rename_outcome_unknown(ready, monkeypatch, capsys):
    """BT-E19f [MLP] rename 시도 뒤 root fd 기준 임시 파일 관측이 실패한다."""
    harness = _harness_for(monkeypatch, ready)
    harness.inject_rename("ledger.json", 0)
    inner_rename, real_lstat = os.rename, os.lstat
    attempted = {"on": False}

    def rename(*args, **kwargs):
        attempted["on"] = True
        return inner_rename(*args, **kwargs)

    def lstat(path, *args, **kwargs):
        if attempted["on"] and str(path) == "ledger.json.tmp":
            raise OSError(errno.EIO, "injected lstat failure")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "lstat", lstat)
    old_bytes = (ready.root / "ledger.json").read_bytes()
    try:
        rc, err = _run(_cmd(ready), capsys)
        _assert_write_failed(rc, err, "ledger rename failed: outcome unknown", "ready")
        assert harness.owners[-1].uncertain is True
        assert (ready.root / "ledger.json").read_bytes() == old_bytes
    finally:
        harness.discard()


def test_bt_e19g_root_fsync_fails(ready, monkeypatch, capsys):
    """BT-E19g [MLP] root 디렉터리 fsync가 실패한다."""
    harness = _harness_for(monkeypatch, ready)
    harness.inject_fd_call("fsync", "root-dir")
    old = _ledger(ready)
    rc, err = _run(_cmd(ready), capsys)
    _assert_write_failed(rc, err, "root fsync failed:", "authorized")
    assert (ready.root / "ledger.json").read_bytes() == _authorized_bytes(old)


def test_bt_e19h_reread_returns_old_bytes_mvp(ready, monkeypatch, capsys):
    """BT-E19h [MVP] 재읽기가 옛 바이트를 돌려준다."""
    from opencrab.stores import _single_file

    real = _single_file.read_binding_fd
    old_bytes = (ready.root / "ledger.json").read_bytes()
    seen = {"ledger": 0}

    def read(directory_fd, name, fd):
        raw = real(directory_fd, name, fd)
        if name == "ledger.json":
            seen["ledger"] += 1
            if seen["ledger"] == 2:
                return old_bytes
        return raw

    monkeypatch.setattr(_single_file, "read_binding_fd", read)
    rc, err = _run(_cmd(ready), capsys)
    _assert_write_failed(rc, err, "ledger re-read differs", "authorized")
    assert seen["ledger"] == 2


def test_bt_e19i_owner_uncertain_before_write(ready, monkeypatch, capsys):
    """BT-E19i [MLP] owner가 uncertain이다."""
    harness = _harness_for(monkeypatch, ready)
    old = (ready.root / "ledger.json").read_bytes()
    _after_preflight(monkeypatch, lambda: setattr(harness.current_owner(), "uncertain", True))
    try:
        rc, err = _run(_cmd(ready), capsys)
        _assert_write_failed(rc, err, "owner is uncertain", "ready")
        assert (ready.root / "ledger.json").read_bytes() == old
        assert not (ready.root / "ledger.json.tmp").exists()
    finally:
        harness.discard()


def test_bt_e19j_root_swapped_before_write(ready, tmp_path, monkeypatch, capsys):
    """BT-E19j [MLP] 점검과 쓰기 사이에 root 이름이 다른 디렉터리를 가리킨다. 점검한 디렉터리에만 쓴다."""
    moved = tmp_path / "published.moved"
    old_bytes = (ready.root / "ledger.json").read_bytes()

    def swap():
        os.rename(ready.root, moved)
        os.mkdir(ready.root)
        shutil.copyfile(moved / "ledger.json", ready.root / "ledger.json")

    _after_preflight(monkeypatch, swap)
    rc, err = _run(_cmd(ready), capsys)
    _assert_write_failed(rc, err, "root directory differs after write", "ready")
    assert (ready.root / "ledger.json").read_bytes() == old_bytes
    assert os.listdir(ready.root) == ["ledger.json"]
    assert (moved / "ledger.json").read_bytes() == _authorized_bytes(json.loads(old_bytes))


def test_bt_e19k_root_swapped_to_empty_directory_reports_unreadable(ready, tmp_path, monkeypatch, capsys):
    """BT-E19k [MLP] 교체된 root에 ledger.json이 없다. 관찰 상태는 unreadable이다(W10 변형)."""
    moved = tmp_path / "published.moved"
    old_bytes = (ready.root / "ledger.json").read_bytes()

    def swap():
        os.rename(ready.root, moved)
        os.mkdir(ready.root)

    _after_preflight(monkeypatch, swap)
    rc, err = _run(_cmd(ready), capsys)
    _assert_write_failed(rc, err, "root directory differs after write", "unreadable")
    assert os.listdir(ready.root) == []
    assert (moved / "ledger.json").read_bytes() == _authorized_bytes(json.loads(old_bytes))


def _close_failure(monkeypatch, ctx):
    harness = _harness_for(monkeypatch, ctx)
    harness.inject_fd_call("close", "root-dir")
    return harness


def test_bt_e20_cleanup_fails_after_apply(ready, monkeypatch, capsys):
    """BT-E20 [MLP] apply 성공 뒤 정리가 실패한다. 주입 지점은 root-dir fd의 실제 close다."""
    harness = _close_failure(monkeypatch, ready)
    old = _ledger(ready)
    try:
        rc, err = _run(_cmd(ready), capsys)
        assert rc == 2, err
        assert any(line.startswith("authorize applied; cleanup failed") for line in _lines(err)), err
        assert (ready.root / "ledger.json").read_bytes() == _authorized_bytes(old)
    finally:
        harness.discard()


def test_bt_e20b_cleanup_fails_after_write_failure(ready, monkeypatch, capsys):
    """BT-E20b [MLP] 쓰기 실패 뒤 정리 실패. 점검 실패 rc 2로 읽히지 않는다."""
    harness = _close_failure(monkeypatch, ready)
    harness.inject_rename("ledger.json", 0)
    old_bytes = (ready.root / "ledger.json").read_bytes()
    try:
        rc, err = _run(_cmd(ready), capsys)
        assert rc == 1, err
        lines = [line for line in _lines(err) if line.startswith("authorize write failed; cleanup failed")]
        assert len(lines) == 1, err
        assert "; first error: " in lines[0] and "not performed" in lines[0], err
        assert lines[0].endswith("; observed ledger state: ready"), err
        assert _PRE not in err
        assert (ready.root / "ledger.json").read_bytes() == old_bytes
    finally:
        harness.discard()


def test_bt_e21_ledger_replaced_before_write(ready, monkeypatch, capsys):
    """BT-E21 [MLP] ledger.json이 같은 내용의 새 inode로 교체된다."""
    ledger = ready.root / "ledger.json"

    def swap():
        copy = ready.root.parent / "ledger.swap"
        shutil.copyfile(ledger, copy)
        os.replace(copy, ledger)

    _after_preflight(monkeypatch, swap)
    rc, err = _run(_cmd(ready), capsys)
    _assert_write_failed(rc, err, "ledger changed before rename", "ready")
    assert _ledger(ready)["state"] == "ready"
    assert not (ready.root / "ledger.json.tmp").exists()


@pytest.mark.parametrize("variant", ["flip", "append"])
def test_bt_e22_billing_digest(ready, capsys, variant):
    """BT-E22 [MLP] billing.db의 한 바이트나 크기를 바꾼다."""
    path = ready.root / "billing.db"
    data = path.read_bytes()
    path.write_bytes((bytes([data[0] ^ 1]) + data[1:]) if variant == "flip" else data + b"x")
    _reject_case(ready, capsys, "billing digest differs")


# ---- 엣지 BT-G ----


def test_bt_g1_second_apply_is_rejected(ready, capsys):
    """BT-G1 [MLP] 연속 두 번 적용. 두 번째는 거부되고 root 바이트가 그대로다."""
    rc, err = _run(_cmd(ready), capsys)
    assert rc == 0, err
    _reject_case(ready, capsys, "ledger state differs")


def test_bt_g2_root_name_with_space_and_korean(source_dir, tmp_path, capsys):  # noqa: F811
    """BT-G2 [MLP] root 이름에 공백과 한글이 든다."""
    ctx = _make_ready(source_dir, tmp_path, capsys, name="게시 root")
    rc, err = _run(_cmd(ctx), capsys)
    assert rc == 0, err
    assert _ledger(ctx)["state"] == "authorized"
    assert sorted(os.listdir(ctx.root)) == sorted(_NAMES)


def test_bt_g3_vec0_root_and_blocked_import(ready, monkeypatch, capsys):
    """BT-G3 [MLP] vec0 표가 있는 root가 성공하고 sqlite_vec를 막으면 거부한다."""
    conn = sqlite3.connect(f"{(ready.root / 'single.db').as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        sql = [row[0] for row in conn.execute("SELECT sql FROM sqlite_master WHERE sql LIKE '%USING vec0%'")]
    finally:
        conn.close()
    assert sql
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    _reject_case(ready, capsys, "sqlite_vec is not importable")
    monkeypatch.delitem(sys.modules, "sqlite_vec")
    rc, err = _run(_cmd(ready), capsys)
    assert rc == 0, err


_CHILD = """\
import importlib.util
import os
import signal
import sys

script, point = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("merge_sqlite_stores", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real_rename, real_fsync = os.rename, os.fsync


def die():
    os.kill(os.getpid(), signal.SIGKILL)


def rename(src, dst, *args, **kwargs):
    if point == "before-rename" and str(dst) == "ledger.json":
        die()
    return real_rename(src, dst, *args, **kwargs)


def fsync(fd):
    if point == "before-dir-fsync" and os.path.isdir("/proc/self/fd/" + str(fd)):
        die()
    return real_fsync(fd)


os.rename = rename
os.fsync = fsync
sys.exit(module.main(sys.argv[3:]))
"""


@pytest.mark.parametrize("point", ["before-rename", "before-dir-fsync"])
def test_bt_g4_killed_at_write_point(ready, tmp_path, capsys, point):
    """BT-G4 [MLP] 강제 중단. ledger는 옛 본문이나 새 본문 전체이고 다음 실행 결과가 8절 표와 같다."""
    child = tmp_path / "authorize_child.py"
    child.write_text(_CHILD)
    env = {**os.environ, "PYTHONPATH": str(_REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    old = _ledger(ready)
    result = subprocess.run([sys.executable, str(child), str(SCRIPT), point, *_cmd(ready)], capture_output=True,
                            text=True, timeout=120, env=env, cwd=str(_REPO), check=False)
    assert result.returncode == -9, (result.returncode, result.stderr)
    state = _ledger(ready)["state"]
    if point == "before-rename":
        assert state == "ready"
        assert (ready.root / "ledger.json.tmp").read_bytes() == _authorized_bytes(old)
        _reject_case(ready, capsys, "root file set differs")
        (ready.root / "ledger.json.tmp").unlink()
        rc, err = _run(_cmd(ready), capsys)
        assert rc == 0, err
        assert (ready.root / "ledger.json").read_bytes() == _authorized_bytes(old)
    else:
        assert state == "authorized"
        assert (ready.root / "ledger.json").read_bytes() == _authorized_bytes(old)
        assert not (ready.root / "ledger.json.tmp").exists()
        _reject_case(ready, capsys, "ledger state differs")


@pytest.mark.parametrize("order", ["sorted", "reversed"])
def test_bt_g5_exact_body(ready, capsys, order):
    """BT-G5 [MLP] 쓴 바이트가 sort_keys 정규 직렬화와 같다. 역순 입력으로도 같아야 한다."""
    old = _ledger(ready)
    if order == "reversed":
        items = list(reversed(sorted(old.items())))
        (ready.root / "ledger.json").write_text(json.dumps(dict(items), separators=(",", ":")))
        assert (ready.root / "ledger.json").read_bytes() != _authorized_bytes(old)
    rc, err = _run(_cmd(ready), capsys)
    assert rc == 0, err
    assert (ready.root / "ledger.json").read_bytes() == json.dumps(
        {**old, "state": "authorized"}, sort_keys=True, separators=(",", ":")).encode()


def test_bt_g8_branch_boundary(tmp_path, monkeypatch, capsys):
    """BT-G8 [MLP] 분기 경계. authorize만 오면 authorize의 사용법 오류이고 값으로 오면 기존 흐름이다."""
    with pytest.raises(SystemExit) as info:
        merge_mod.main(["authorize"])
    err = capsys.readouterr().err
    assert info.value.code == 2
    assert err.startswith(_USAGE), err
    # 후반부는 구현 전후 모두 통과하는 회귀 단언이다.
    from tests.test_merge_sqlite_stores import _conn

    directory = tmp_path / "authorize"
    directory.mkdir()
    for name in merge_mod.FILES:
        conn = _conn(directory / name)
        conn.execute(f"CREATE TABLE t_{name.split('.')[0]} (id INTEGER)")
        conn.commit()
        conn.close()
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "merged.db"
    rc = merge_mod.main(["--source-dir", "authorize", "--out", str(out)])
    capsys.readouterr()
    assert rc == 0
    assert not out.exists()
