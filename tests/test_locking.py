"""Regression tests for the shared local write-lock boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from opencrab.locking import file_lock


def test_file_lock_is_reentrant(tmp_path):
    with file_lock("write.lock", str(tmp_path)):
        with file_lock("write.lock", str(tmp_path)):
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
    time.sleep(0.35)
"""
    env = {"PYTHONPATH": "."}
    first = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], env=env)
    try:
        time.sleep(0.05)
        with pytest.raises(TimeoutError):
            with file_lock("write.lock", str(tmp_path), timeout=0.05):
                pass
    finally:
        first.wait(timeout=2)


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
        second = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), str(second_marker)],
            env=env,
            check=True,
        )
        assert second.returncode == 0
        assert time.monotonic() - started >= 0.25
        assert second_marker.exists()
    finally:
        first.wait(timeout=2)


def test_file_lock_creates_explicit_dir_and_normalizes_symlinks(tmp_path):
    real_dir = tmp_path / "real"
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir, target_is_directory=True)

    with file_lock("write.lock", str(link_dir)):
        assert (real_dir / "write.lock").exists()
        with file_lock("write.lock", str(real_dir)):
            pass
