"""Regression tests for the shared local write-lock boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from opencrab.locking import acquire_file_lock, file_lock, write_lock


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
