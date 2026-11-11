"""#627: MCP ``tools/call`` 인자 이름 검증.

스키마 밖 인자와 필수 인자 누락은 ``dispatch_tool``이 도구 함수를 부르기 전에
거른다. 응답은 거부된 인자 이름을 담고, 로그에는 ERROR와 Traceback이 남지
않는다. 도구 본문이 던진 예외는 종전대로 ERROR와 Traceback으로 남는다.

정상, 오류, 엣지 경우를 클래스로 나눈다. 받아들임 기준과 시험의 대응은 PR
본문에 적는다.
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from opencrab.auth import Principal, principal_scope
from opencrab.mcp.server import MCPServer
from opencrab.mcp.tools import UnknownToolError, dispatch_tool
from opencrab.mcp.tools._registry import _REGISTRY, AccessTier, tool
from opencrab.stores.sql_store import SQLStore

_REGISTRY_LOGGER = "opencrab.mcp.tools._registry"
_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _call(name, arguments, *, modern=False, notification=False):
    params = {"name": name, "arguments": arguments}
    if modern:
        params["_meta"] = dict(_MODERN_META)
    request = {"jsonrpc": "2.0", "method": "tools/call", "params": params}
    if not notification:
        request["id"] = 1
    return request


def _payload(response):
    """도구 결과의 content 본문(JSON)과 ``isError`` 값을 돌려준다."""
    result = response["result"]
    return json.loads(result["content"][0]["text"]), result.get("isError")


def _error_or_traceback_records(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info]


def _registry_warnings(caplog):
    return [
        r for r in caplog.records if r.name == _REGISTRY_LOGGER and r.levelno == logging.WARNING
    ]


@pytest.fixture
def server(bind_test_principal):
    """stdio와 HTTP가 함께 쓰는 ``handle_request``를 직접 부른다."""
    return MCPServer()


@pytest.fixture
def http_env(tmp_path, monkeypatch):
    """HTTP 두 앱이 tmp_path의 저장소로 토큰을 확인하게 한다."""
    from opencrab.config import get_settings
    from opencrab.mcp import tools

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    for name in ("OPENCRAB_API_KEY", "LOCALCRAB_MCP_TOKEN", "LOCALCRAB_MCP_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    tools._context.clear()
    yield tmp_path
    get_settings.cache_clear()
    tools._context.clear()


def _bearer():
    from opencrab.auth import bootstrap_local_user
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_sql_store

    _user_id, secret = bootstrap_local_user(make_sql_store(get_settings()))
    return {"Authorization": f"Bearer {secret}"}


@contextmanager
def _app(which):
    """MCP 인스턴스(``opencrab serve --transport http``)와 REST 프로세스의 앱.

    REST 앱은 새로 import한다. ``apps.api.main``은 import할 때 ``mcp_router()``를
    한 번 붙이고, 그 라우터는 처음 성공한 인증 저장소를 캐시한다. 앞선 시험이 다른
    ``LOCAL_DATA_DIR``로 이 모듈을 먼저 썼으면 이 시험의 토큰은 401이 된다. 끝나면
    ``sys.modules``와 패키지 속성을 원래대로 돌린다(``tests/test_http_app_modern.py``의
    import 시험과 같은 방식).
    """
    if which == "serve":
        from opencrab.mcp.http_app import create_app

        yield create_app()
        return
    import apps.api as api_pkg

    name = "apps.api.main"
    original_module = sys.modules.pop(name, None)
    had_attr = hasattr(api_pkg, "main")
    original_attr = getattr(api_pkg, "main", None)
    try:
        yield importlib.import_module(name).app
    finally:
        sys.modules.pop(name, None)
        if original_module is not None:
            sys.modules[name] = original_module
        if had_attr:
            api_pkg.main = original_attr
        elif hasattr(api_pkg, "main"):
            delattr(api_pkg, "main")


# ---------------------------------------------------------------------------
# 정상
# ---------------------------------------------------------------------------


class TestNormal:
    @pytest.mark.parametrize("modern", [False, True], ids=["legacy", "modern"])
    def test_schema_conforming_call_runs_without_rejection_warning(self, server, caplog, modern):
        caplog.set_level(logging.WARNING)
        response = server.handle_request(_call("ontology_manifest", {}, modern=modern))
        payload, is_error = _payload(response)
        assert "spaces" in payload
        assert is_error in (None, False)
        assert _registry_warnings(caplog) == []

    @pytest.mark.parametrize("modern", [False, True], ids=["legacy", "modern"])
    @pytest.mark.parametrize(
        "exc_type", [TypeError, RuntimeError], ids=["TypeError", "RuntimeError"]
    )
    def test_handler_internal_exception_keeps_error_and_traceback(
        self, server, caplog, modern, exc_type
    ):
        """대조: 도구 본문이 던진 예외는 종전 응답과 ERROR+Traceback 기록을 유지한다.

        본문 안의 TypeError도 넣는다. 인자 오류를 받는 좁은 catch가
        TypeError 전체로 넓어지면 이 경우가 ERROR 없이 사라진다.
        """
        caplog.set_level(logging.WARNING)
        exc = exc_type("handler internal failure")
        graph = MagicMock()
        graph.get_node_by_id_scoped.side_effect = exc
        del graph.run_cypher
        context = {"neo4j": graph, "sql": SQLStore("sqlite:///:memory:")}
        with patch("opencrab.mcp.tools._get_context", return_value=context):
            response = server.handle_request(
                _call("ontology_get_node", {"node_id": "n1"}, modern=modern)
            )
        payload, is_error = _payload(response)
        assert payload == {"error": f"ontology_get_node failed ({exc_type.__name__})"}
        if modern:
            assert is_error is True
        logged = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and r.exc_info and r.exc_info[1] is exc
        ]
        assert len(logged) == 1
        assert _registry_warnings(caplog) == []


# ---------------------------------------------------------------------------
# 오류
# ---------------------------------------------------------------------------


class TestError:
    @pytest.mark.parametrize("modern", [False, True], ids=["legacy", "modern"])
    def test_unexpected_argument_is_named_without_error_log_stdio(self, server, caplog, modern):
        caplog.set_level(logging.WARNING)
        response = server.handle_request(
            _call("ontology_list_edges", {"from_type": "Concept"}, modern=modern)
        )
        payload, is_error = _payload(response)
        assert "from_type" in payload["error"]
        if modern:
            assert is_error is True
        assert _error_or_traceback_records(caplog) == []
        warnings = _registry_warnings(caplog)
        assert len(warnings) == 1
        assert "ontology_list_edges" in warnings[0].getMessage()
        assert "from_type" not in warnings[0].getMessage()

    @pytest.mark.parametrize("which", ["serve", "apps_api"])
    def test_unexpected_argument_is_named_without_error_log_http(self, http_env, caplog, which):
        """MCP 인스턴스 둘이 쓰는 앱과 REST 프로세스의 ``/mcp``가 같은 결과를 낸다."""
        caplog.set_level(logging.WARNING)
        headers = _bearer()
        with _app(which) as app:
            resp = TestClient(app).post(
                "/mcp",
                json=_call("ontology_list_edges", {"from_type": "Concept", "to_type": "Concept"}),
                headers=headers,
            )
        assert resp.status_code == 200
        payload, _ = _payload(resp.json())
        assert "from_type" in payload["error"]
        assert "to_type" in payload["error"]
        assert _error_or_traceback_records(caplog) == []
        warnings = _registry_warnings(caplog)
        assert len(warnings) == 1
        assert "to_type" not in warnings[0].getMessage()

    @pytest.mark.parametrize("modern", [False, True], ids=["legacy", "modern"])
    def test_missing_required_argument_is_named_without_error_log(self, server, caplog, modern):
        caplog.set_level(logging.WARNING)
        response = server.handle_request(_call("ontology_get_node", {}, modern=modern))
        payload, is_error = _payload(response)
        assert "node_id" in payload["error"]
        if modern:
            assert is_error is True
        assert _error_or_traceback_records(caplog) == []
        assert len(_registry_warnings(caplog)) == 1

    def test_rejected_before_handler_and_write_lock(self, bind_test_principal):
        """거부는 도구 함수 호출과 write.lock 획득보다 먼저 일어난다."""
        from opencrab.mcp.tools import ToolArgumentError

        calls: list[int] = []
        events: list[str] = []

        class _FakeLock:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, *exc_info):
                events.append("exit")

        schema = {
            "description": "probe",
            "inputSchema": {
                "type": "object",
                "properties": {"x": {"type": "integer"}},
                "required": ["x"],
            },
        }
        name = "__issue627_write_probe__"
        assert name not in _REGISTRY
        try:

            @tool(name, schema, access=AccessTier.WRITE, writes=True)
            def _probe(x):
                calls.append(x)
                return {"ok": True}

            with patch("opencrab.mcp.tools._write_lock", return_value=_FakeLock()):
                with pytest.raises(ToolArgumentError) as unexpected:
                    dispatch_tool(name, {"x": 1, "y_extra": 2})
                with pytest.raises(ToolArgumentError) as missing:
                    dispatch_tool(name, {})
                assert calls == []
                assert events == []

                assert dispatch_tool(name, {"x": 1}) == {"ok": True}
                assert calls == [1]
                assert events == ["enter", "exit"]
        finally:
            _REGISTRY.pop(name, None)

        assert isinstance(unexpected.value, TypeError)
        assert "y_extra" in str(unexpected.value)
        assert "['x']" in str(missing.value)


# ---------------------------------------------------------------------------
# 엣지
# ---------------------------------------------------------------------------


class TestEdge:
    def test_unexpected_and_missing_are_both_named(self, server, caplog):
        caplog.set_level(logging.WARNING)
        response = server.handle_request(_call("ontology_get_node", {"nodeId": "n1"}))
        payload, _ = _payload(response)
        assert "nodeId" in payload["error"]
        assert "node_id" in payload["error"]
        assert _error_or_traceback_records(caplog) == []

    def test_legacy_null_arguments_reports_missing_required(self, server, caplog):
        """legacy는 ``arguments: null``을 ``{}``로 정규화한 뒤 같은 검증을 받는다."""
        caplog.set_level(logging.WARNING)
        response = server.handle_request(_call("ontology_get_node", None))
        payload, _ = _payload(response)
        assert "node_id" in payload["error"]
        assert _error_or_traceback_records(caplog) == []

    def test_notification_with_unexpected_argument_is_silent_without_error_log(
        self, server, caplog
    ):
        caplog.set_level(logging.WARNING)
        response = server.handle_request(
            _call("ontology_list_edges", {"from_type": "Concept"}, notification=True)
        )
        assert response is None
        assert _error_or_traceback_records(caplog) == []

    @pytest.mark.parametrize("modern", [False, True], ids=["legacy", "modern"])
    def test_rejection_warning_failure_keeps_input_error(self, server, caplog, modern):
        """기록 경로가 예외를 올려도 응답은 인자 이름을 담은 입력 오류다.

        ``docs/observability-rules.md``: 관측 오류가 원래 작업의 예외를
        대체하지 않는다.
        """
        from opencrab.mcp.tools import _registry

        caplog.set_level(logging.WARNING)
        with patch.object(_registry.logger, "warning", side_effect=OSError("log sink down")):
            response = server.handle_request(
                _call("ontology_list_edges", {"from_type": "Concept"}, modern=modern)
            )
        payload, is_error = _payload(response)
        assert "from_type" in payload["error"]
        if modern:
            assert is_error is True
        assert _error_or_traceback_records(caplog) == []

    def test_hidden_admin_tool_with_unexpected_argument_stays_unknown(self):
        """#150: 권한 밖 도구는 인자 검증보다 먼저 미지 도구로 끝난다.

        인자 오류 메시지는 허용 인자 이름을 담으므로, 순서가 바뀌면 숨긴
        도구의 존재와 스키마가 원격 호출자에게 드러난다.
        """
        remote = Principal(user_id="remote-627", is_local=False, disabled=False)
        with principal_scope(remote):
            with pytest.raises(UnknownToolError) as exc_info:
                dispatch_tool("schema_pack_install", {"name": "saas", "bogus_arg": 1})
        assert "bogus_arg" not in str(exc_info.value)
