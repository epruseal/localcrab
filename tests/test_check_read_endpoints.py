"""``scripts/check_read_endpoints.py``: 전환 뒤 읽기 검증 도구 (#710).

실제 소켓 서버 대역(stdlib http.server)으로 mcp와 api 두 형태의 합격과 실패, 토큰 비유출,
기대 팩 집합 대조, 설정 오류를 확인한다. 이 도구는 쓰기 호출을 보내지 않아야 한다.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_read_endpoints.py"
_spec = importlib.util.spec_from_file_location("check_read_endpoints", SCRIPT)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

TOKEN = "lc_SECRET_TOKEN_VALUE_123"
PACKS = ["pack-a", "pack-b"]


class Fake:
    """서버 대역의 동작 설정과 받은 요청 기록."""

    def __init__(self):
        self.packs = list(PACKS)
        self.anonymous_status = 401
        self.token_status = 200
        self.tool_is_error = False
        self.legacy_error_tool = None  # 이 도구는 200과 {"error": ...} 본문을 돌려준다(isError 키 없음).
        self.rpc_error = False
        self.sse = False
        self.echo_token_in_error = False
        self.redirect_to = None
        self.requests = []


def _handler(fake: Fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, payload=None, text=None):
            body = text if text is not None else json.dumps(payload)
            raw = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self) -> bool:
            has = self.headers.get("Authorization") == f"Bearer {TOKEN}"
            if not has:
                if fake.echo_token_in_error and self.headers.get("Authorization"):
                    self._send(401, text="bad credential " + self.headers["Authorization"])
                else:
                    self._send(fake.anonymous_status, {"error": "unauthorized"})
                return fake.anonymous_status == 200
            if fake.token_status != 200:
                self._send(fake.token_status, {"error": "denied"})
                return False
            return True

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length)) if length else None

        def do_GET(self):
            fake.requests.append(("GET", self.path))
            if fake.redirect_to:
                self.send_response(302)
                self.send_header("Location", fake.redirect_to)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._auth():
                return
            if self.path == "/api/packs":
                self._send(200, {"packs": [{"pack_id": p} for p in fake.packs]})
            elif self.path == "/api/status":
                self._send(200, {"ok": True})
            else:
                self._send(404, {})

        def do_POST(self):
            body = self._body()
            fake.requests.append(("POST", self.path, (body or {}).get("params", {}).get("name")))
            if not self._auth():
                return
            if self.path == "/api/query":
                self._send(200, {"results": [1, 2]})
                return
            name = body["params"]["name"]
            if fake.rpc_error:
                self._send(200, {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32000, "message": "x"}})
                return
            if name == fake.legacy_error_tool:
                text = json.dumps({"error": "backfill in progress"})
                self._send(200, {"jsonrpc": "2.0", "id": body["id"],
                                 "result": {"content": [{"type": "text", "text": text}]}})
                return
            inner = ({"results": [1]} if name == "ontology_query"
                     else {"packs": [{"pack_id": p} for p in fake.packs]})
            result = {"content": [{"type": "text", "text": json.dumps(inner)}],
                      "isError": fake.tool_is_error}
            message = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            if fake.sse:
                self._send(200, text="event: message\ndata: " + json.dumps(message) + "\n\n")
            else:
                self._send(200, message)

    return Handler


@pytest.fixture
def server():
    fake = Fake()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    fake.port = httpd.server_address[1]
    yield fake
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def token_env(monkeypatch):
    monkeypatch.setenv("TEST_READ_TOKEN", TOKEN)


def _run(server: Fake, shape: str, *extra: str) -> int:
    return tool.main([str(server.port), shape, "--token-env", "TEST_READ_TOKEN", *extra])


def _assert_no_leak(capsys) -> str:
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    return captured.out


@pytest.mark.parametrize("shape", ["mcp", "api"])
def test_pass_prints_pack_ids_and_never_the_token(server, capsys, shape):
    assert _run(server, shape) == 0
    out = _assert_no_leak(capsys)
    assert "pack-a pack-b" in out
    assert "RESULT: PASS" in out


def test_mcp_reads_server_sent_event_replies(server, capsys):
    server.sse = True
    assert _run(server, "mcp") == 0
    _assert_no_leak(capsys)


@pytest.mark.parametrize("shape", ["mcp", "api"])
def test_only_read_calls_are_sent(server, shape):
    assert _run(server, shape) == 0
    for request in server.requests:
        if request[0] == "POST" and request[1] == "/mcp":
            assert request[2] in {"ontology_query", "content_pack_list"}
        else:
            assert request[1] in {"/api/packs", "/api/status", "/api/query"}


@pytest.mark.parametrize("shape", ["mcp", "api"])
def test_anonymous_request_accepted_is_a_failure(server, capsys, shape):
    server.anonymous_status = 200
    assert _run(server, shape) == 1
    _assert_no_leak(capsys)


@pytest.mark.parametrize("shape", ["mcp", "api"])
def test_token_rejected_is_a_failure_and_leaks_nothing(server, capsys, shape):
    server.token_status = 401
    server.echo_token_in_error = True
    assert _run(server, shape) == 1
    _assert_no_leak(capsys)


def test_tool_error_flag_is_a_failure(server, capsys):
    server.tool_is_error = True
    assert _run(server, "mcp") == 1
    _assert_no_leak(capsys)


@pytest.mark.parametrize("tool_name", ["ontology_query", "content_pack_list"])
def test_legacy_error_body_without_is_error_is_a_failure(server, capsys, tool_name):
    server.legacy_error_tool = tool_name
    assert _run(server, "mcp") == 1
    _assert_no_leak(capsys)


def test_json_rpc_error_envelope_is_a_failure(server, capsys):
    server.rpc_error = True
    assert _run(server, "mcp") == 1
    _assert_no_leak(capsys)


@pytest.mark.parametrize("shape", ["mcp", "api"])
def test_proxy_environment_is_ignored(server, monkeypatch, capsys, shape):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{dead}")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    # urllib는 opener를 만들 때 프록시 환경변수를 읽는다. 환경을 정한 뒤 모듈을 새로 적재한다.
    fresh_spec = importlib.util.spec_from_file_location("check_read_endpoints_fresh", SCRIPT)
    fresh = importlib.util.module_from_spec(fresh_spec)
    fresh_spec.loader.exec_module(fresh)
    assert fresh.main([str(server.port), shape, "--token-env", "TEST_READ_TOKEN"]) == 0
    _assert_no_leak(capsys)


@pytest.mark.parametrize("shape", ["mcp", "api"])
def test_connection_refused_is_a_failure_without_token(capsys, shape):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert tool.main([str(port), shape, "--token-env", "TEST_READ_TOKEN"]) == 1
    _assert_no_leak(capsys)


def test_redirect_is_not_followed(server, capsys):
    other = Fake()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(other))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        server.redirect_to = f"http://127.0.0.1:{httpd.server_address[1]}/api/packs"
        assert _run(server, "api") == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert other.requests == []
    _assert_no_leak(capsys)


@pytest.mark.parametrize("shape", ["mcp", "api"])
def test_expected_set_missing_pack_fails_and_extra_pack_passes(server, tmp_path, capsys, shape):
    expected = tmp_path / "expected.txt"
    expected.write_text("pack-a\npack-b\npack-gone\n")
    assert _run(server, shape, "--expected", str(expected)) == 1
    assert "expected_minus_got: ['pack-gone']" in capsys.readouterr().out
    expected.write_text("pack-a\n")
    assert _run(server, shape, "--expected", str(expected)) == 0
    assert "got_minus_expected: ['pack-b']" in capsys.readouterr().out


def test_save_expected_writes_list_and_refuses_overwrite(server, tmp_path):
    saved = tmp_path / "saved.txt"
    assert _run(server, "api", "--save-expected", str(saved)) == 0
    assert saved.read_text() == "pack-a\npack-b\n"
    assert _run(server, "api", "--save-expected", str(saved)) == 2
    assert saved.read_text() == "pack-a\npack-b\n"


def test_empty_pack_list_round_trips_through_expected(server, tmp_path):
    server.packs = []
    saved = tmp_path / "saved.txt"
    assert _run(server, "mcp", "--save-expected", str(saved)) == 0
    assert _run(server, "mcp", "--expected", str(saved)) == 0


def test_token_from_client_config(server, tmp_path, capsys):
    config = tmp_path / "client.json"
    config.write_text(json.dumps({"mcpServers": {"crab": {"headers": {"Authorization": f"Bearer {TOKEN}"}}}}))
    assert tool.main([str(server.port), "api", "--client-config", str(config), "--server", "crab"]) == 0
    _assert_no_leak(capsys)
    config.write_text(json.dumps({"mcpServers": {"crab": {"headers": {"Authorization": TOKEN}}}}))
    assert tool.main([str(server.port), "api", "--client-config", str(config), "--server", "crab"]) == 0


@pytest.mark.parametrize("content", ["not json", "{}", '{"mcpServers": {"crab": {}}}',
                                    '{"mcpServers": {"crab": {"headers": {"Authorization": 5}}}}'])
def test_bad_client_config_is_a_usage_error(server, tmp_path, content, capsys):
    config = tmp_path / "client.json"
    config.write_text(content)
    assert tool.main([str(server.port), "api", "--client-config", str(config), "--server", "crab"]) == 2
    _assert_no_leak(capsys)


def test_missing_client_config_file_is_a_usage_error(server, tmp_path):
    missing = tmp_path / "none.json"
    assert tool.main([str(server.port), "api", "--client-config", str(missing)]) == 2


def test_empty_or_unset_token_env_is_a_usage_error(server, monkeypatch):
    monkeypatch.setenv("TEST_READ_TOKEN", "  ")
    assert _run(server, "api") == 2
    monkeypatch.delenv("TEST_READ_TOKEN")
    assert _run(server, "api") == 2


def test_token_source_is_required():
    with pytest.raises(SystemExit) as exc:
        tool.main(["1234", "api"])
    assert exc.value.code == 2


def test_remote_host_is_refused_without_flag(server, capsys):
    assert _run(server, "api", "--host", "192.0.2.1") == 2
    assert server.requests == []
    _assert_no_leak(capsys)


def test_bad_port_is_a_usage_error():
    assert tool.main(["70000", "api", "--token-env", "TEST_READ_TOKEN"]) == 2


def test_missing_expected_file_is_a_usage_error(server, tmp_path):
    assert _run(server, "api", "--expected", str(tmp_path / "none.txt")) == 2
