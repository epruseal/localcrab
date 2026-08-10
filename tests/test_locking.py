"""Regression tests for the shared local write-lock boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from opencrab.locking import file_lock


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
