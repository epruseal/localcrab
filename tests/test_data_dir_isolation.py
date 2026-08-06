"""Regression guard for issue #126.

The test suite must never be able to reach the user's real default data
directory (``~/.local/share/localcrab``), even if an individual test forgets
to isolate itself, AND even if the invoking shell has already exported
``LOCAL_DATA_DIR`` to a real (possibly production) path before pytest starts
— that is exactly the scenario a mere ``setdefault`` would have missed.
``tests/conftest.py`` therefore *overrides* ``LOCAL_DATA_DIR`` unconditionally
to a session-scoped temp dir; this file pins that behaviour so a future test
(or a reverted conftest change) cannot silently regress it.

The concrete reachable choke point is ``opencrab.mcp.tools._write_lock()``:
every ``dispatch_tool`` call for a ``writes=True`` tool goes through it
regardless of whether the caller mocked ``_get_context``, and it resolves its
lock-file directory via ``LOCAL_DATA_DIR`` (falling back to
``get_settings().local_data_dir``, the real HOME-derived default, when unset).
"""

from __future__ import annotations

import os
from pathlib import Path


def test_session_default_is_the_forced_session_temp_dir():
    """conftest.py must have overridden LOCAL_DATA_DIR to its own session temp
    dir — not merely "some value other than the HOME-derived default". This
    is the check that actually catches a shell that pre-exported LOCAL_DATA_DIR
    to a real (e.g. production) path: only an exact match to the value
    conftest.py itself created proves the override, not a passthrough, won."""
    import conftest

    forced = Path(os.environ["LOCAL_DATA_DIR"]).resolve()
    assert forced == Path(conftest._TEST_DATA_DIR).resolve()

    # Belt-and-suspenders: also confirm it isn't the HOME-derived default.
    from opencrab.config import _default_local_data_dir

    assert forced != Path(_default_local_data_dir()).resolve()


def test_write_lock_never_touches_real_data_dir():
    """The write.lock choke point must not create/touch anything under the
    real default data dir when a test doesn't set LOCAL_DATA_DIR itself."""
    from opencrab.config import _default_local_data_dir
    from opencrab.mcp.tools import _write_lock

    real_default = Path(_default_local_data_dir())
    before = _snapshot(real_default)

    with _write_lock():
        pass

    after = _snapshot(real_default)
    assert before == after, (
        "a write-tool dispatch touched the real default data dir "
        f"({real_default}) — isolation in tests/conftest.py regressed"
    )


def _snapshot(directory: Path) -> dict[str, float] | None:
    if not directory.exists():
        return None
    return {p.name: p.stat().st_mtime_ns for p in directory.iterdir()}
