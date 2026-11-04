"""#608: a ?token= value must not reach the REST process's logs.

``apps.api.main`` mounts ``mcp_router()`` with ``allow_query_token=False``, so it
answers 401 to a query credential. uvicorn logs the request line before that
answer, so the value reached the access log of this process although the MCP
instance (``opencrab serve``) masked it (#427). The redaction install lives in
``mcp_router()`` now, and both apps reach it through that call.

The process test starts the real entry point (``python -m apps.api.main``) in a
subprocess with an isolated HOME and data directory, sends requests that carry
a fake secret generated at run time, and asserts on the raw stdout and stderr.
"""

from __future__ import annotations

import importlib.util
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
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(
            (
                f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        s.settimeout(5)
        try:
            return s.recv(4096)
        except OSError:
            return b""


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
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("OPENCRAB_", "LOCAL_", "MCP_", "PORT"))},
        "HOME": str(home),
        "LOCAL_DATA_DIR": str(data),
        "LOCALCRAB_ENV_FILE": os.devnull,
        "PORT": str(port),
        "PYTHONUNBUFFERED": "1",
    }
    with out.open("wb") as fo, err.open("wb") as fe:
        proc = subprocess.Popen(
            [sys.executable, "-m", "apps.api.main"],
            stdout=fo,
            stderr=fe,
            env=env,
            cwd=str(_REPO_ROOT),
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
    # WebSocket Upgrade carrying the secret, rejected by the app.
    _ws_upgrade(port, f"/mcp?token={secret}")

    stdout, stderr = read_logs()

    # Logging is alive (control), so absence of the secret is meaningful.
    assert "marker=anon" in stdout
    assert "/mcp?token=***" in stdout
    assert "/api/usage?token=***" in stdout
    assert "x=1&token=***" in stdout
    if importlib.util.find_spec("websockets") is not None:
        # Positive control for the uvicorn.error channel.
        assert 'WebSocket /mcp?token=***"' in stderr
    for text in (stdout, stderr):
        assert secret not in text
