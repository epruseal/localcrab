"""Regression tests for the shared local write-lock boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from opencrab.locking import acquire_file_lock, file_lock, write_lock, write_lock_busy_message


def test_file_lock_is_reentrant(tmp_path):
    # timeout= is load-bearing: without the re-entrancy branch the inner
    # acquire would BLOCK on the lock this thread already holds, and the
    # test would hang forever instead of failing. The timeout turns that
    # deadlock into a TimeoutError the runner reports.
    with file_lock("write.lock", str(tmp_path)):
        with file_lock("write.lock", str(tmp_path), timeout=2):
            assert (tmp_path / "write.lock").exists()


def test_file_lock_releases_after_exception(tmp_path):
    try:
        with file_lock("write.lock", str(tmp_path)):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with file_lock("write.lock", str(tmp_path), timeout=1):
        pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX timeout behavior")
def test_file_lock_honors_timeout(tmp_path):
    script = """
import sys, time
from opencrab.locking import file_lock
with file_lock('write.lock', sys.argv[1]):
    open(sys.argv[2], 'w').close()
    time.sleep(0.35)
"""
    marker = tmp_path / "held"
    env = {"PYTHONPATH": "."}
    first = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(marker)], env=env)
    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        with pytest.raises(TimeoutError):
            with file_lock("write.lock", str(tmp_path), timeout=0.05):
                pass
    finally:
        first.wait(timeout=2)


def test_file_lock_honors_timeout_for_other_thread(tmp_path):
    ready = threading.Event()
    release = threading.Event()

    def hold_lock():
        with file_lock("write.lock", str(tmp_path)):
            ready.set()
            release.wait(timeout=2)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert ready.wait(timeout=2)
    try:
        with pytest.raises(TimeoutError):
            with file_lock("write.lock", str(tmp_path), timeout=0.05):
                pass
    finally:
        release.set()
        holder.join(timeout=2)


def test_file_lock_releases_process_guard_when_open_fails(tmp_path):
    lock_path = tmp_path / "write.lock"
    lock_path.mkdir()
    with pytest.raises(IsADirectoryError):
        with file_lock("write.lock", str(tmp_path)):
            pass
    lock_path.rmdir()
    with file_lock("write.lock", str(tmp_path)):
        pass


def test_file_lock_serializes_subprocesses(tmp_path):
    script = """
import sys, time
from opencrab.locking import file_lock
with file_lock('write.lock', sys.argv[1]):
    open(sys.argv[2], 'w').close()
    time.sleep(0.35)
"""
    first_marker = tmp_path / "first"
    second_marker = tmp_path / "second"
    env = {"PYTHONPATH": "."}
    first = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(first_marker)], env=env
    )
    try:
        deadline = time.monotonic() + 2
        while not first_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first_marker.exists()

        started = time.monotonic()
        subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), str(second_marker)],
            env=env,
            check=True,
        )
        assert time.monotonic() - started >= 0.25
        assert second_marker.exists()
    finally:
        first.wait(timeout=2)


def test_file_lock_creates_explicit_dir_and_normalizes_symlinks(tmp_path):
    real_dir = tmp_path / "real"
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    with file_lock("write.lock", str(link_dir)):
        # The directory did not exist before this call — file_lock() created
        # it, and created it at the symlink TARGET, not beside the link.
        assert (real_dir / "write.lock").exists()
        assert not (tmp_path / "link" / "write.lock").is_symlink()

        # timeout= is load-bearing here too. Without realpath normalisation
        # the two spellings key to different in-process locks, so this inner
        # acquire would take a SECOND flock on the same inode from the same
        # process and block forever rather than fail.
        with file_lock("write.lock", str(real_dir), timeout=2):
            pass

    # Exactly one lock file for the two spellings: proof they normalised to
    # one path rather than each creating their own.
    assert sorted(p.name for p in real_dir.iterdir()) == ["write.lock"]


# ---------------------------------------------------------------------------
# issue #69: an omitted timeout used to mean "wait forever" on flock(LOCK_EX).
# A thread holding the SAME path's threading.RLock (opencrab.locking's
# _process_locks) already blocks a second acquirer before it ever reaches
# fcntl, so a plain thread holder is enough to observe the bound below --
# no subprocess needed, matching tests/test_write_lock_ownership_141.py's
# established pattern for this file.
# ---------------------------------------------------------------------------


class _Holder:
    """Hold a named file lock on a thread until told to let go."""

    def __init__(self, filename: str, data_dir: str) -> None:
        self.filename = filename
        self.data_dir = data_dir
        self.holding = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with file_lock(self.filename, self.data_dir):
            self.holding.set()
            self.release.wait(timeout=5)

    def __enter__(self) -> _Holder:
        self.thread.start()
        assert self.holding.wait(timeout=5), "lock holder thread never acquired the lock"
        return self

    def __exit__(self, *exc: object) -> None:
        self.release.set()
        self.thread.join(timeout=5)


@pytest.fixture
def short_write_lock_timeout(monkeypatch):
    """Shrink WRITE_LOCK_TIMEOUT so the default-timeout tests run fast."""
    from opencrab.config import get_settings

    monkeypatch.setenv("WRITE_LOCK_TIMEOUT", "0.2")
    get_settings.cache_clear()
    yield 0.2
    get_settings.cache_clear()


def test_file_lock_omitted_timeout_is_bounded_not_infinite(tmp_path, short_write_lock_timeout):
    """정상: timeout 생략은 더 이상 무제한 대기가 아니다(#69)."""
    with _Holder("write.lock", str(tmp_path)):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with file_lock("write.lock", str(tmp_path)):
                pass
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"기본 타임아웃이 걸리지 않았다: {elapsed}s"


def test_acquire_file_lock_omitted_timeout_is_bounded(tmp_path, short_write_lock_timeout):
    """정상: acquire_file_lock() 도 같은 기본값을 받는다(#69)."""
    with _Holder("write.lock", str(tmp_path)):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            acquire_file_lock("write.lock", str(tmp_path))
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"기본 타임아웃이 걸리지 않았다: {elapsed}s"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_file_lock_rejects_non_finite_or_negative_explicit_timeout(tmp_path, bad):
    """오류: NaN·inf·음수 timeout 은 명시적으로 줘도 거부된다(#291 흡수)."""
    with pytest.raises(ValueError):
        with file_lock("write.lock", str(tmp_path), timeout=bad):
            pass


@pytest.mark.parametrize("bad_env", ["nan", "inf", "-1"])
def test_default_lock_wait_timeout_rejects_poisoned_config(tmp_path, monkeypatch, bad_env):
    """오류: WRITE_LOCK_TIMEOUT 설정값 자체가 오염돼도 기본 경로에서 거부된다(#291)."""
    from opencrab.config import get_settings

    monkeypatch.setenv("WRITE_LOCK_TIMEOUT", bad_env)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError):
            with file_lock("write.lock", str(tmp_path)):
                pass
    finally:
        get_settings.cache_clear()


def test_file_lock_explicit_timeout_still_passes_through(tmp_path, short_write_lock_timeout):
    """엣지: 명시적 timeout 은 기본값 대신 그대로 쓰인다."""
    with _Holder("write.lock", str(tmp_path)):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with file_lock("write.lock", str(tmp_path), timeout=2):
                pass
        elapsed = time.monotonic() - started
        # 짧은 기본값(0.2s)보다 명시값(2s)에 가깝게 걸려야 한다 -- 기본값이
        # 명시값을 덮어쓰지 않는다는 증거.
        assert elapsed >= 1.5, f"명시 timeout 이 기본값으로 대체됐다: {elapsed}s"


def test_write_lock_busy_message_names_write_lock(tmp_path, short_write_lock_timeout):
    """정상: write_lock() 의 타임아웃 메시지가 write.lock 을 지목한다."""
    with _Holder("write.lock", str(tmp_path)):
        with pytest.raises(TimeoutError, match="write.lock"):
            with write_lock(str(tmp_path)):
                pass


def test_write_lock_does_not_rewrap_body_timeout_error(tmp_path):
    """엣지: 보호 구간 안에서 난 TimeoutError 는 획득 실패 메시지로 바뀌지 않는다."""
    with pytest.raises(TimeoutError, match="unrelated body error"):
        with write_lock(str(tmp_path)):
            raise TimeoutError("unrelated body error")


def test_write_lock_for_own_file_lock_omitted_timeout_is_bounded(tmp_path, short_write_lock_timeout):
    """정상: write_lock_for_store(own_file=True) 의 개별 파일 락도 같은 기본값을 받는다."""
    from opencrab.stores.sql_store import write_lock_for_store

    class _FakeStore:
        _is_sqlite = True
        _url = f"sqlite:///{tmp_path}/billing.db"

    lock_name = "billing.db.lock"
    with _Holder(lock_name, str(tmp_path)):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with write_lock_for_store(_FakeStore(), own_file=True):
                pass
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"own_file 락에 기본 타임아웃이 걸리지 않았다: {elapsed}s"


def test_mcp_write_lock_uses_shared_default_timeout(tmp_path, short_write_lock_timeout, monkeypatch):
    """정상: mcp/tools 의 _write_lock() 이 write_lock() 을 경유해 같은 보장을 받는다(#69 원 지점)."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    from opencrab.mcp.tools import _write_lock

    with _Holder("write.lock", str(tmp_path)):
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="write.lock"):
            with _write_lock():
                pass
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"_write_lock() 이 무제한으로 대기했다: {elapsed}s"


# ---------------------------------------------------------------------------
# issue #352: write.lock 파일에 보유자 레코드(pid/획득 시각/purpose)를 남긴다.
# 레코드는 진단용이다 -- flock 의미론은 바뀌지 않고, 레코드 읽기/쓰기의 어떤
# 실패도 획득과 해제를 막지 않는다(design-v8.md 0절 불변식).
# ---------------------------------------------------------------------------


def _read_raw_record(lock_path: str) -> dict:
    """테스트 전용: 락을 잡지 않고 락 파일 바이트를 그대로 JSON으로 읽는다."""
    with open(lock_path, "rb") as fh:
        return json.loads(fh.read())


def test_acquire_records_holder_on_polling_branch(tmp_path):
    """정상(1a): 공개 경로(file_lock, 폴링 분기)로 배타 획득하면 pid/시각이 남는다."""
    lock_path = str(tmp_path / "write.lock")
    with file_lock("write.lock", str(tmp_path), shared=False, timeout=1.0):
        record = _read_raw_record(lock_path)
    assert record["pid"] == os.getpid()
    from datetime import datetime

    datetime.fromisoformat(record["started_at"])  # 파싱되면 통과
    assert "purpose" not in record


def test_acquire_records_holder_on_immediate_blocking_branch(tmp_path):
    """정상(1b): `_acquire()`의 즉시 블로킹 분기(timeout=None)는 공개 진입점으로
    도달 불가능하므로(둘 다 `_resolve_timeout()`이 항상 구체 float를 만들어
    넘긴다), `_acquire()`를 직접 불러 이 분기를 강제로 태운다(v6 라운드 1 지적)."""
    from opencrab.locking import _acquire, _lock_path, _open_lock, _release

    lock_path = _lock_path("write.lock", str(tmp_path))
    fh = _open_lock(lock_path)
    try:
        _acquire(fh, shared=False, timeout=None)
        record = _read_raw_record(lock_path)
    finally:
        _release(fh)
        fh.close()
    assert record["pid"] == os.getpid()
    from datetime import datetime

    datetime.fromisoformat(record["started_at"])
    assert "purpose" not in record


def test_acquire_immediate_blocking_branch_shared_does_not_record(tmp_path):
    """정상(1b의 shared 변형, 14절 c1의 유일한 검출 경로): `_acquire()`의 즉시
    블로킹 분기(timeout=None)도, 공유 획득에서는 레코드를 쓰면 안 된다. 두
    반환점의 `if not shared:` 가드는 서로 다른 두 줄이므로(v6 라운드 1 지적),
    폴링 분기를 보는 테스트 3과는 별개로 이 분기 전용 검출 경로가 필요하다."""
    from opencrab.locking import _acquire, _lock_path, _open_lock, _release

    lock_path = _lock_path("chroma.lock", str(tmp_path))
    sentinel = b"untouched-bytes"
    with open(lock_path, "wb") as f:
        f.write(sentinel)
    fh = _open_lock(lock_path)
    try:
        _acquire(fh, shared=True, timeout=None)
    finally:
        _release(fh)
        fh.close()
    assert open(lock_path, "rb").read() == sentinel


def test_shared_acquisition_never_records_holder(tmp_path):
    """정상(3): shared=True 획득은 레코드를 남기지 않는다 -- 여러 리더가 동시에
    쥘 수 있어 "단일 보유자"라는 전제 자체가 성립하지 않는다(#140)."""
    lock_path = tmp_path / "chroma.lock"
    sentinel = b"untouched-bytes"
    lock_path.write_bytes(sentinel)
    for _ in range(3):
        with file_lock("chroma.lock", str(tmp_path), shared=True, timeout=1.0):
            pass
    assert lock_path.read_bytes() == sentinel


def test_write_lock_purpose_lands_in_the_record(tmp_path):
    """정상(2a): write_lock(purpose=...)로 감싼 구간에서 레코드에 purpose가 있다."""
    lock_path = str(tmp_path / "write.lock")
    with write_lock(str(tmp_path), purpose="mcp tool: pack_ingest"):
        record = _read_raw_record(lock_path)
    assert record["purpose"] == "mcp tool: pack_ingest"


def test_dispatch_tool_write_purpose_reaches_the_record(tmp_path, monkeypatch, bind_test_principal):
    """정상(2b): `dispatch_tool()`을 실제로 통과시켜 purpose가
    `f"mcp tool: {name}"`으로 락 파일까지 이어짐을 증명한다 -- `_write_lock()`이나
    `write_lock()`을 대신 호출하는 우회 없이, 실제 도구 호출부 한 줄까지 확인한다."""
    import contextvars
    import dataclasses

    from opencrab.mcp.tools import dispatch_tool
    from opencrab.mcp.tools._registry import _REGISTRY

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    lock_path = str(tmp_path / "write.lock")

    entered = threading.Event()
    release = threading.Event()

    def _blocking_write_tool(**kwargs):
        entered.set()
        release.wait(timeout=5)
        return {"ok": True}

    name = "ontology_add_node"
    original = _REGISTRY[name]
    _REGISTRY[name] = dataclasses.replace(original, fn=_blocking_write_tool)
    try:
        # threading.Thread starts with a FRESH contextvars.Context -- the
        # principal bound (via principal_scope, a ContextVar) by the
        # bind_test_principal fixture in THIS (main) thread would not be
        # visible to dispatch_tool() running on a plain new thread. Copy the
        # current context explicitly so the worker sees the same principal.
        ctx = contextvars.copy_context()
        worker = threading.Thread(target=ctx.run, args=(dispatch_tool, name, {}))
        worker.start()
        try:
            assert entered.wait(timeout=5), "쓰기 도구 본문에 들어가지 않았다"
            record = _read_raw_record(lock_path)
            assert record["purpose"] == f"mcp tool: {name}"
        finally:
            release.set()
            worker.join(timeout=5)
    finally:
        _REGISTRY[name] = original


def test_record_survives_release(tmp_path):
    """정상(4): release 이후에도 방금 쓴 레코드는 지워지지 않는다."""
    lock_path = str(tmp_path / "write.lock")
    with write_lock(str(tmp_path)):
        pass
    record = _read_raw_record(lock_path)
    assert record["pid"] == os.getpid()


def test_record_write_leaves_no_trailing_bytes(tmp_path):
    """엣지(5): purpose 있는(긴) 레코드 뒤에 purpose 없는(짧은) 레코드를 다시 쓰면
    결과 바이트가 새 레코드의 json.dumps()와 정확히 같다 -- ftruncate 누락은
    이전 레코드의 꼬리를 남긴다."""
    lock_path = tmp_path / "write.lock"
    with write_lock(str(tmp_path), purpose="a very long purpose string, much longer than the next"):
        pass
    with write_lock(str(tmp_path)):
        pass
    raw = lock_path.read_bytes()
    record = json.loads(raw)
    assert raw == json.dumps(record).encode("utf-8")
    assert "purpose" not in record


def test_write_lock_purpose_does_not_leak_across_success(tmp_path):
    """정상(g1): purpose 있는 write_lock() 이 성공적으로 끝난 직후, purpose 없이
    다른 배타 락을 잡아도 이전 purpose가 섞여 들어가지 않는다.

    두 번째 획득은 `write_lock()`이 아니라 `file_lock()`을 직접 쓴다.
    `write_lock()`은 자기 호출마다 진입 즉시 `_held.pending_purpose = purpose`로
    스스로 재설정하므로, 두 번째 획득도 `write_lock()`을 쓰면 첫 번째 호출이
    정리를 했는지와 무관하게 항상 통과해 이 테스트가 아무것도 검출하지 못한다
    (실측: `finally` 절 전체를 지운 되돌림에도 이 구조로는 통과했다 -- 14절
    g의 실제 검출력 확인 과정에서 드러난 결함, design-v8.md 원안을 수정)."""
    with write_lock(str(tmp_path), purpose="leaked-purpose-g1"):
        pass
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_path = str(other_dir / "write.lock")
    with file_lock("write.lock", str(other_dir), shared=False, timeout=1.0):
        pass
    record = _read_raw_record(other_path)
    assert "purpose" not in record


def test_write_lock_purpose_does_not_leak_across_timeout(tmp_path):
    """정상(g2): purpose 있는 write_lock() 이 TimeoutError로 끝난 직후, purpose
    없이 다른 배타 락을 잡아도 이전 purpose가 섞여 들어가지 않는다.

    g1과 마찬가지로 두 번째 획득은 `file_lock()`을 직접 쓴다(위 g1 docstring
    참조). g1만으로는 `finally`가 아니라 `try` 블록의 성공 분기에만 clear를
    넣은 미묘하게 다른 오구현(성공 시엔 지우지만 TimeoutError 시엔 안 지움)을
    놓친다 -- g1은 통과시키고 g2만 실패시키는 그 오구현을 이 테스트가 가른다
    (design-v8.md 14절 g2, 라운드 2 지적)."""
    with _Holder("write.lock", str(tmp_path)):
        with pytest.raises(TimeoutError):
            with write_lock(str(tmp_path), timeout=0.05, purpose="leaked-purpose-g2"):
                pass
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_path = str(other_dir / "write.lock")
    with file_lock("write.lock", str(other_dir), shared=False, timeout=1.0):
        pass
    record = _read_raw_record(other_path)
    assert "purpose" not in record


def test_dispatch_timeout_error_excludes_holder_info_at_modern_boundary(
    tmp_path, monkeypatch, bind_test_principal, short_write_lock_timeout, caplog
):
    """정상(6a): `_modern_tools_call()`을 통해 실제로 write.lock 타임아웃을
    유발하면 응답 봉투에는 pid/시작 시각/purpose가 없다. `dispatch_tool()` 자체는
    `{"error": ...}` 봉투를 만들지 않으므로(그 봉투는 server.py의 두 핸들러가
    각자 만든다, v6 라운드 1 지적), 실제 반환 구조인
    `{"content": [{"type": "text", "text": "<JSON 문자열>"}], ...}`를
    `content[0]["text"]`부터 재파싱해서 확인한다(라운드 2 지적: 실측으로 확인된
    실제 구조). 같은 정보가 `logger.warning`쪽에는 있음을 함께 확인해, 경계가
    "차단"이 아니라 "분리"임을 증명한다."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    from opencrab.mcp.server import MCPServer

    server = MCPServer()
    with _Holder("write.lock", str(tmp_path)):
        with caplog.at_level("WARNING"):
            response = server._modern_tools_call({"name": "ontology_add_node", "arguments": {}})
    text = response["content"][0]["text"]
    assert json.loads(text) == {"error": "ontology_add_node failed (TimeoutError)"}
    assert "pid=" not in text
    warning_text = " ".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "pid=" in warning_text


def test_dispatch_timeout_error_excludes_holder_info_at_legacy_boundary(
    tmp_path, monkeypatch, bind_test_principal, short_write_lock_timeout, caplog
):
    """정상(6b): `_handle_tools_call()`(레거시)을 통해 같은 확인을 반복한다.
    같은 JSON 재파싱 경로를 쓰되, 검증 대상 핸들러가 다르다(v6 라운드 1 지적)."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    from opencrab.mcp.server import MCPServer

    server = MCPServer()
    with _Holder("write.lock", str(tmp_path)):
        with caplog.at_level("WARNING"):
            response = server._handle_tools_call({"name": "ontology_add_node", "arguments": {}})
    text = response["content"][0]["text"]
    assert json.loads(text) == {"error": "ontology_add_node failed (TimeoutError)"}
    assert "pid=" not in text
    warning_text = " ".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "pid=" in warning_text


def test_busy_message_reports_truncated_record_as_unknown(tmp_path, caplog):
    """엣지(7): 락 파일에 잘린 JSON이 있어도 예외 없이 "Holder: unknown" 계열
    문구가 반환되고, `_read_holder_record()`가 남긴 debug 로그가 잡힌다."""
    lock_path = tmp_path / "write.lock"
    lock_path.write_bytes(b'{"pid": 123, "started')
    with caplog.at_level("DEBUG", logger="opencrab.locking"):
        message = write_lock_busy_message(str(lock_path), 1.0)
    assert "Holder: unknown" in message
    assert any("failed to read lock holder record" in r.message for r in caplog.records)


def test_busy_message_reports_non_dict_record_as_no_readable_record(tmp_path):
    """엣지(h2 보조): dict가 아닌 유효 JSON(`[]`)은 "no readable record" 이지
    "record present but unrecognized" 가 아니다 -- 후자는 record가 dict인데
    인식 필드가 하나도 없을 때(예: `{}`)만 나온다(라운드 2 지적, v7 초판 정정)."""
    lock_path = tmp_path / "write.lock"
    lock_path.write_bytes(b"[]")
    message = write_lock_busy_message(str(lock_path), 1.0)
    assert "Holder: unknown (no readable record)." in message

    lock_path.write_bytes(b"{}")
    message = write_lock_busy_message(str(lock_path), 1.0)
    assert "Holder: unknown (record present but unrecognized)." in message


def _hold_write_lock_report_pid(data_dir: str, ready, stop, pid_queue) -> None:
    """회귀(8)용 자식 프로세스 본체. 모듈 스코프인 이유는
    tests/test_chroma_lock_ownership.py의 `_hold_chroma_lock`과 같다: fork가
    아닌 시작 방식에서 로컬 함수는 피클링에 실패한다."""
    from opencrab.locking import write_lock

    with write_lock(data_dir):
        pid_queue.put(os.getpid())
        ready.set()
        stop.wait(30)


@pytest.mark.skipif(os.name == "nt", reason="POSIX multiprocessing + flock 경합")
def test_write_lock_timeout_names_the_real_holder_pid_across_processes(tmp_path):
    """회귀(요구사항 3): 진짜 별도 OS 프로세스가 write.lock을 쥔 상태에서 타임아웃
    나면, 에러 메시지의 pid가 그 자식 프로세스의 실제 pid와 같다.

    핸드셰이크(Event)로 "자식이 이미 락을 잡았다"를 확인한 뒤에만 부모가
    시도한다(v6 라운드 1 지적: 타이밍만 믿으면 거짓 성공이 날 수 있다). 자식
    회수는 `finally`에 둔다(라운드 2 지적: 끝에서만 회수하면 중간 단언 실패
    시 자식이 남는다). 전체를 signal.alarm() 안전망으로 감싼다."""
    import multiprocessing
    import signal

    def _on_alarm(signum, frame):
        raise TimeoutError("자식 프로세스 핸드셰이크 또는 write_lock() 시도가 멈춘 것으로 보인다")

    ready = multiprocessing.Event()
    stop = multiprocessing.Event()
    pid_queue = multiprocessing.Queue()
    child = multiprocessing.Process(
        target=_hold_write_lock_report_pid, args=(str(tmp_path), ready, stop, pid_queue)
    )
    child.start()
    old_handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(30)
    try:
        try:
            assert ready.wait(20), "자식이 write.lock을 잡았다는 신호가 오지 않았다"
            child_pid = pid_queue.get(timeout=10)
            with pytest.raises(TimeoutError) as exc_info:
                with write_lock(str(tmp_path), timeout=1.0):
                    pass
            assert f"pid={child_pid}" in str(exc_info.value)
            assert child_pid == child.pid
        finally:
            stop.set()
            child.join(10)
            if child.is_alive():
                child.terminate()
                child.join(5)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
