"""#427: a ?token= credential must not reach the server's own logs.

Channels covered (see docs/mcp-client-auth.md): ``uvicorn.access`` (request
line), ``uvicorn.asgi`` (TRACE scope dump) and ``uvicorn.error`` (WebSocket
handshake lines, closed by ``ws="none"`` in ``opencrab serve``).

The integration tests start the real ``opencrab serve --transport http
--allow-query-token`` in a subprocess with an isolated HOME and data
directory, send requests carrying a fake secret generated at run time, and
assert on the raw stdout and stderr the server wrote.
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
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from opencrab.mcp.http_app import AccessLogTokenRedactor, install_access_log_redaction

# ---------------------------------------------------------------------------
# Unit: the filter
# ---------------------------------------------------------------------------


def _record(*args, name="uvicorn.access") -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, "%s", args, None)


def _access_path(path: str) -> str:
    rec = _record("127.0.0.1:1", "POST", path, "1.1", 401)
    assert AccessLogTokenRedactor().filter(rec) is True
    return rec.args[2]


def test_masks_token_value():
    assert _access_path("/mcp?token=SECRETVALUE") == "/mcp?token=***"


def test_masks_percent_encoded_parameter_name():
    # Starlette decodes the name, so ?%74oken= authenticates and must be masked.
    assert _access_path("/mcp?%74oken=SECRETVALUE") == "/mcp?%74oken=***"
    assert _access_path("/mcp?to%6ben=SECRETVALUE") == "/mcp?to%6ben=***"


def test_masks_every_repeated_token_parameter():
    assert _access_path("/mcp?token=AAA&x=1&token=BBB") == "/mcp?token=***&x=1&token=***"


def test_keeps_other_parameters_and_lookalike_names():
    assert _access_path("/mcp?a=1&b=2") == "/mcp?a=1&b=2"
    # These names do not authenticate, so they are not treated as the token.
    assert _access_path("/mcp?Token=keep&access_token=keep&to+ken=keep") == (
        "/mcp?Token=keep&access_token=keep&to+ken=keep"
    )


def test_path_without_query_is_untouched():
    assert _access_path("/healthz") == "/healthz"


def test_unexpected_args_shapes_pass_through():
    f = AccessLogTokenRedactor()
    rec = _record()
    assert f.filter(rec) is True
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "%(a)s", ({"a": "?token=keep"},), None)
    assert f.filter(rec) is True  # dict-style args are not rewritten, never raises


def test_asgi_trace_scope_query_string_is_masked_on_a_copy():
    scope = {"type": "http", "path": "/mcp", "query_string": b"token=SECRETVALUE&x=1", "raw_path": b"/mcp"}
    rec = _record("ASGI", 1, scope, name="uvicorn.asgi")
    AccessLogTokenRedactor().filter(rec)
    assert rec.args[2]["query_string"] == b"token=***&x=1"
    assert scope["query_string"] == b"token=SECRETVALUE&x=1"  # the live scope is not mutated


def test_install_is_idempotent_and_attaches_both_loggers():
    install_access_log_redaction()
    install_access_log_redaction()
    for name in ("uvicorn.access", "uvicorn.asgi"):
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, AccessLogTokenRedactor)]
        assert len(filters) == 1


# ---------------------------------------------------------------------------
# CLI wiring: ws="none" reaches uvicorn.run from the real `serve` command
# ---------------------------------------------------------------------------


def test_serve_http_disables_websocket_protocol():
    from opencrab.cli import main

    with patch("uvicorn.run") as run, patch("opencrab.mcp.http_app.create_app", return_value=MagicMock()):
        result = CliRunner().invoke(
            main, ["serve", "--transport", "http", "--port", "1", "--allow-query-token"]
        )
    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["ws"] == "none"


# ---------------------------------------------------------------------------
# Integration: real server process, captured stdout and stderr
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, headers: dict[str, str] | None = None, method: str = "GET", data: bytes | None = None):
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=data)
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


@pytest.fixture(params=["info", "trace"])
def served(request, tmp_path):
    """Run the real serve command; yield (port, secret, read_logs())."""
    home = tmp_path / "home"
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    port = _free_port()
    out = tmp_path / "stdout.log"
    err = tmp_path / "stderr.log"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("OPENCRAB_", "LOCAL_", "MCP_"))},
        "HOME": str(home),
        "LOCAL_DATA_DIR": str(data),
        "LOCALCRAB_ENV_FILE": os.devnull,
        "LOG_LEVEL": request.param,
        "PYTHONUNBUFFERED": "1",
    }
    with out.open("wb") as fo, err.open("wb") as fe:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "opencrab.cli",
                "serve",
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--allow-query-token",
            ],
            stdout=fo,
            stderr=fe,
            env=env,
            cwd=str(tmp_path),
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    pytest.fail(f"server exited early: {err.read_text()[-2000:]}")
                try:
                    if _get(f"http://127.0.0.1:{port}/healthz") == 200:
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                pytest.fail("server did not become healthy")
            secret = "T" + secrets.token_hex(12)

            def read_logs() -> tuple[str, str]:
                # Give the server a moment to flush the last lines.
                time.sleep(0.5)
                return out.read_text(errors="replace"), err.read_text(errors="replace")

            yield port, secret, request.param, read_logs
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def test_query_token_never_reaches_server_logs(served):
    port, secret, level, read_logs = served
    base = f"http://127.0.0.1:{port}"
    # Leak channel: secret in the URL (POST and GET, plain and trailing slash).
    _get(f"{base}/mcp?token={secret}", method="POST", data=b"{}")
    _get(f"{base}/mcp/?token={secret}", method="GET")
    # Control: header auth. Control: no token at all.
    _get(f"{base}/mcp?marker=header", headers={"Authorization": f"Bearer {secret}"}, method="POST", data=b"{}")
    _get(f"{base}/mcp?marker=anon", method="POST", data=b"{}")
    # WebSocket Upgrade carrying the secret, rejected by the server.
    _ws_upgrade(port, f"/mcp?token={secret}")

    stdout, stderr = read_logs()

    # Logging is alive (controls), so absence of the secret is meaningful.
    assert "marker=header" in stdout
    assert "marker=anon" in stdout
    assert "/mcp?token=***" in stdout
    if level == "trace":
        # Positive control for the uvicorn.asgi channel.
        assert "Started scope" in stderr
        assert "token=***" in stderr
    for text in (stdout, stderr):
        assert secret not in text
