"""#608: a ?token= value must not reach the REST process's logs.

``apps.api.main`` mounts ``mcp_router()`` with ``allow_query_token=False``, so it
answers 401 to a query credential. uvicorn logs the request line before that
answer, so the value reached the access log of this process although the MCP
instance (``opencrab serve``) masked it (#427). The redaction install lives in
``mcp_router()`` now, and both apps reach it through that call.

The process test starts the real entry point (``apps.api.main`` as ``__main__``)
in a subprocess, sends requests that carry a fake secret generated at run time,
and asserts on the raw stdout and stderr. The child cannot read a ``.env`` file
or an inherited store setting (see ``_child_env`` and ``_RUNNER``).
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from opencrab.mcp.http_app import AccessLogTokenRedactor, mcp_router

_UVICORN_LOGGERS = ("uvicorn.access", "uvicorn.asgi", "uvicorn.error")
_REPO_ROOT = Path(__file__).resolve().parent.parent

# ``apps/api/main.py`` calls ``load_dotenv`` for two ``.env`` files. This runner
# makes both calls no-ops and then runs the module as ``__main__``, the same
# code path as ``python -m apps.api.main`` (the module name and the
# ``uvicorn.run`` call are the same).
_RUNNER = (
    "import dotenv, runpy; "
    "dotenv.load_dotenv = lambda *a, **k: False; "
    'runpy.run_module("apps.api.main", run_name="__main__")'
)


def test_mcp_router_installs_the_redactor_on_every_uvicorn_logger():
    saved = {}
    for name in _UVICORN_LOGGERS:
        target = logging.getLogger(name)
        saved[name] = list(target.filters)
        for f in saved[name]:
            if isinstance(f, AccessLogTokenRedactor):
                target.removeFilter(f)
    try:
        mcp_router()
        mcp_router()  # idempotent: the REST app and `serve` each build one router per process
        for name in _UVICORN_LOGGERS:
            found = [f for f in logging.getLogger(name).filters if isinstance(f, AccessLogTokenRedactor)]
            assert len(found) == 1, name
    finally:
        for name, filters in saved.items():
            target = logging.getLogger(name)
            for f in list(target.filters):
                if isinstance(f, AccessLogTokenRedactor):
                    target.removeFilter(f)
            for f in filters:
                if f not in target.filters:
                    target.addFilter(f)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(url: str, method: str = "GET", data: bytes | None = None) -> int:
    req = urllib.request.Request(url, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _ws_upgrade(port: int, target: str) -> bytes:
    """Send a WebSocket upgrade request and return the HTTP status line.

    A connection error, a timeout, or a reply that ends before the first CRLF
    raises. The caller never reads a failed read as a skipped control.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(
            (
                f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        s.settimeout(5)
        reply = b""
        while b"\r\n" not in reply:
            chunk = s.recv(4096)
            if not chunk:
                raise AssertionError(f"connection closed before a status line: {reply!r}")
            reply += chunk
        return reply.split(b"\r\n", 1)[0]


def _child_env(home: Path, data: Path, port: int) -> dict[str, str]:
    """Environment of the REST child: an allow list, not a filtered copy.

    No inherited store setting (``STORAGE_MODE``, ``POSTGRES_URL``, ...) reaches
    the child, so it opens only the local stores under ``data``. This helper
    does not reuse the isolation values of tests/conftest.py. conftest sets
    ``LOCALCRAB_ENV_FILE`` with ``setdefault``, so a caller can point it at a
    real file, and its ``LOCAL_DATA_DIR`` is one directory for the whole session.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LOCAL_DATA_DIR": str(data),
        "LOCALCRAB_ENV_FILE": os.devnull,
        "PORT": str(port),
        "PYTHONPATH": str(_REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
    }


@pytest.fixture
def rest_process(tmp_path):
    """Run the real REST entry point; yield (port, secret, read_logs())."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    port = _free_port()
    out = tmp_path / "stdout.log"
    err = tmp_path / "stderr.log"
    # The working directory holds no ``.env``: Pydantic Settings reads one from
    # the current directory (``opencrab.config._default_env_files``).
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = _child_env(home, data, port)
    with out.open("wb") as fo, err.open("wb") as fe:
        proc = subprocess.Popen(
            [sys.executable, "-c", _RUNNER],
            stdout=fo,
            stderr=fe,
            env=env,
            cwd=str(cwd),
        )
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    pytest.fail(f"REST process exited early: {err.read_text()[-2000:]}")
                try:
                    if _request(f"http://127.0.0.1:{port}/openapi.json") == 200:
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                pytest.fail("REST process did not become ready")
            secret = "T" + secrets.token_hex(12)

            def read_logs() -> tuple[str, str]:
                # Give the process a moment to flush the last lines.
                time.sleep(0.5)
                return out.read_text(errors="replace"), err.read_text(errors="replace")

            yield port, secret, read_logs
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def test_query_token_never_reaches_rest_process_logs(rest_process):
    port, secret, read_logs = rest_process
    base = f"http://127.0.0.1:{port}"
    # Leak channel: secret in the URL. The MCP route, a REST route and an unknown path.
    assert _request(f"{base}/mcp?token={secret}", method="POST", data=b"{}") == 401
    assert _request(f"{base}/mcp/?token={secret}") == 401
    assert _request(f"{base}/api/usage?token={secret}") == 401
    assert _request(f"{base}/no-such-path?x=1&token={secret}") == 404
    # Control: no token at all.
    _request(f"{base}/mcp?marker=anon", method="POST", data=b"{}")
    # WebSocket Upgrade carrying the secret.
    ws_status = _ws_upgrade(port, f"/mcp?token={secret}")

    stdout, stderr = read_logs()

    # Logging is alive (control), so absence of the secret is meaningful.
    assert "marker=anon" in stdout
    assert "/mcp?token=***" in stdout
    assert "/api/usage?token=***" in stdout
    assert "x=1&token=***" in stdout
    if ws_status.startswith(b"HTTP/1.1 403"):
        # uvicorn handled the handshake and the app refused it. Positive
        # control for the uvicorn.error channel.
        assert 'WebSocket /mcp?token=***"' in stderr
    else:
        # Without a WebSocket library uvicorn hands the upgrade request to the
        # HTTP path, and the missing credential answers 401. Any other status
        # line is a failure.
        assert ws_status.startswith(b"HTTP/1.1 401"), ws_status
    for text in (stdout, stderr):
        assert secret not in text
