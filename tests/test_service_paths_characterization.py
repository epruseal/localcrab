"""
Characterization tests for the interface service layer (MCP / CLI / HTTP).

이 파일은 곧 진행될 "인터페이스 서비스 계층 추출" 리팩토링의 회귀 안전망이다.
이상적 동작이 아니라 **현재 코드의 실제 입출력을 그대로 박제**한다. 리팩토링이
동작을 바꾸면 이 테스트가 깨져야 한다.

세 인터페이스가 공유하는(따라서 통합 대상인) 로직:
  1. pack 선택   — auto_pack / pack_ids 우선순위 / 임계값 미달 / include_unpackaged 무효 경고
  2. query 경로  — 응답 envelope 구조 (selected_packs / pack_filter / keyword_fallback)
  3. node/edge 쓰기 — builder 경유(MCP) vs 멀티스토어 직접 쓰기(HTTP)

설계 노트
---------
* node/edge 쓰기 경로는 결정적이므로 **실제 LocalGraphStore/LocalSQLDocStore/SQLStore/
  ChromaStore**를 tmp_path 위에 띄워 실제 반환 dict를 박제한다 (기존 test_query_keyword_local
  픽스처 패턴 + builder 직결).
* query 경로는 ChromaStore 임베딩에 의존해 비결정적이므로, 기존 test_mcp_pack_aware.py가
  쓰는 **hybrid=MagicMock** 방식으로 envelope 구조/pack 분기만 박제한다. 결과 값은 박지 않는다.
* CLI / HTTP는 빈 store에서 결과가 결정적으로 비므로 그 경우의 응답 형식을 박제한다.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencrab.ontology.query import QueryResult

REPO_ROOT = Path(__file__).resolve().parents[1]


# ===========================================================================
# Shared fixtures
# ===========================================================================


@pytest.fixture()
def local_env(tmp_path, monkeypatch):
    """LOCAL_DATA_DIR/STORAGE_MODE를 tmp_path 로컬 모드로 고정하고 settings 캐시를 초기화."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    monkeypatch.setenv("OPENCRAB_STORAGE_MODE", "local")
    from opencrab.config import get_settings

    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()
    yield tmp_path
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()


@pytest.fixture()
def local_stores(local_env):
    """실제 로컬 백엔드 4종 (graph/docs/sql/vector). 외부 서버 연결 없음."""
    from opencrab.config import get_settings
    from opencrab.stores.factory import (
        make_doc_store,
        make_graph_store,
        make_sql_store,
        make_vector_store,
    )

    cfg = get_settings()
    return {
        "graph": make_graph_store(cfg),
        "docs": make_doc_store(cfg),
        "sql": make_sql_store(cfg),
        "vector": make_vector_store(cfg),
    }


@pytest.fixture()
def builder(local_stores):
    from opencrab.ontology.builder import OntologyBuilder

    return OntologyBuilder(
        local_stores["graph"],
        local_stores["docs"],
        local_stores["sql"],
        vec=local_stores["vector"],
    )


@pytest.fixture()
def mcp_local_ctx(local_stores, builder):
    """MCP _get_context()가 반환하는 형태의 실 로컬 ctx (빌더 경유 쓰기 박제용)."""
    from opencrab.billing.hooks import BillingHooks
    from opencrab.ontology.impact import ImpactEngine
    from opencrab.ontology.query import HybridQuery
    from opencrab.ontology.rebac import ReBACEngine

    g = local_stores["graph"]
    s = local_stores["sql"]
    hybrid = HybridQuery(local_stores["vector"], g)
    hybrid._doc_store = local_stores["docs"]
    rebac = ReBACEngine(g, s)
    hybrid._rebac = rebac
    return {
        "neo4j": g,
        "chroma": local_stores["vector"],
        "mongo": local_stores["docs"],
        "sql": s,
        "builder": builder,
        "rebac": rebac,
        "impact": ImpactEngine(g, s),
        "hybrid": hybrid,
        "billing": BillingHooks(s),
    }


def _make_query_result(node_id: str = "n1", pack_id: str | None = "pack-a") -> QueryResult:
    meta: dict = {"node_id": node_id}
    if pack_id:
        meta["pack_id"] = pack_id
    return QueryResult(source="vector", node_id=node_id, score=0.9, text="alpha", metadata=meta)


def _mock_query_ctx(results):
    """ontology_query envelope 분기 박제용: hybrid만 MagicMock, 나머지는 stub."""
    hybrid = MagicMock()
    hybrid.query.return_value = results
    billing = MagicMock()
    billing.on_query = MagicMock()
    return {
        "neo4j": MagicMock(),
        "chroma": MagicMock(),
        "mongo": MagicMock(),
        "sql": MagicMock(),
        "builder": MagicMock(),
        "rebac": MagicMock(),
        "impact": MagicMock(),
        "hybrid": hybrid,
        "billing": billing,
    }


def _write_pack_manifest(data_dir: Path, pack_id: str, **fields) -> None:
    """auto_pack이 선택할 수 있도록 <data_dir>/packs/<pack_id>/stage/manifest.json 작성."""
    stage = data_dir / "packs" / pack_id / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    manifest = {"pack_id": pack_id, "counts": {"nodes": 1}}
    manifest.update(fields)
    (stage / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture()
def api_module():
    """apps/api/main.py 를 파일 경로로 로드 (apps는 패키지가 아니라 import 불가)."""
    spec = importlib.util.spec_from_file_location(
        "api_main_characterization", REPO_ROOT / "apps" / "api" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def http_client(local_env, api_module, monkeypatch):
    """TestClient with lifespan entered. raise_server_exceptions=False로 500도 응답으로 관찰."""
    monkeypatch.setenv("OPENCRAB_API_KEY", "testkey")
    monkeypatch.setenv("OPENCRAB_TIER", "free")
    from fastapi.testclient import TestClient

    with TestClient(api_module.app, raise_server_exceptions=False) as client:
        yield client


HTTP_AUTH = {"Authorization": "Bearer testkey", "X-User-Id": "tester"}


# ===========================================================================
# 1. Pack selection logic
#    MCP(ontology_query) 와 CLI(query) 가 공유하는 choose_packs + load_pack_registry 분기
# ===========================================================================


class TestPackSelectionMCP:
    """MCP ontology_query 의 auto_pack / pack_ids 우선순위 / 임계값 / include_unpackaged 분기."""

    def test_auto_pack_selects_single_pack(self, local_env):
        _write_pack_manifest(
            local_env,
            "nemotron-pack",
            title="Nemotron Pack",
            description="about nemotron",
            keywords=["nemotron"],
        )
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([_make_query_result(pack_id="nemotron-pack")])
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("tell me about nemotron", auto_pack=True)

        # 정확히 1개 선택, pack_id 박제. score 값은 키워드/alias 가중 합이라 구조만 검증.
        assert len(resp["selected_packs"]) == 1
        assert resp["selected_packs"][0]["pack_id"] == "nemotron-pack"
        assert isinstance(resp["selected_packs"][0]["score"], float)
        assert isinstance(resp["selected_packs"][0]["matched"], list)
        assert resp["pack_filter"]["pack_ids"] == ["nemotron-pack"]
        assert resp["pack_filter"]["auto_pack"] is True
        assert resp["pack_filter"]["include_unpackaged"] is False
        # 정상 선택 시 warnings 키는 없다.
        assert "warnings" not in resp["pack_filter"]
        # auto_pack이 effective_pack_ids를 hybrid.query에 전달했는지.
        assert ctx["hybrid"].query.call_args.kwargs["pack_ids"] == ["nemotron-pack"]

    def test_pack_ids_take_priority_over_auto_pack(self, local_env):
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([_make_query_result(pack_id="pack-a")])
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("q", pack_ids=["pack-a"], auto_pack=True)

        assert resp["pack_filter"]["pack_ids"] == ["pack-a"]
        assert resp["pack_filter"]["auto_pack"] is False
        assert resp["pack_filter"]["warnings"] == ["pack_ids provided; ignoring auto_pack"]
        # 명시 pack_ids 우선이므로 selected_packs는 비어 있다 (auto 선택 미수행).
        assert resp["selected_packs"] == []

    def test_auto_pack_below_threshold_falls_back(self, local_env):
        _write_pack_manifest(
            local_env,
            "nemotron-pack",
            title="Nemotron Pack",
            description="about nemotron",
            keywords=["nemotron"],
        )
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([])
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("totally unrelated random words", auto_pack=True)

        assert resp["selected_packs"] == []
        assert resp["pack_filter"]["pack_ids"] is None
        assert resp["pack_filter"]["auto_pack"] is True
        assert resp["pack_filter"]["warnings"] == [
            "auto_pack could not select a pack above the score threshold; "
            "falling back to full-store search"
        ]

    def test_auto_pack_no_registry_falls_back(self, local_env):
        """packs 디렉토리가 없으면 (registry 비어 있음) 임계값 미달과 동일하게 fallback."""
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([])
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("anything", auto_pack=True)

        assert resp["selected_packs"] == []
        assert resp["pack_filter"]["pack_ids"] is None
        assert "warnings" in resp["pack_filter"]

    def test_include_unpackaged_without_pack_filter_warns(self, local_env):
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([])
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("q", include_unpackaged=True)

        assert resp["pack_filter"]["include_unpackaged"] is True
        assert resp["pack_filter"]["warnings"] == [
            "include_unpackaged has no effect without pack_ids/auto_pack"
        ]

    def test_include_unpackaged_with_pack_ids_no_warning(self, local_env):
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([_make_query_result(pack_id="pack-a")])
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("q", pack_ids=["pack-a"], include_unpackaged=True)

        assert resp["pack_filter"]["include_unpackaged"] is True
        assert "warnings" not in resp["pack_filter"]
        assert ctx["hybrid"].query.call_args.kwargs["include_unpackaged"] is True


class TestPackSelectionCLI:
    """CLI query --json-envelope 가 MCP와 동일한 choose_packs 로직을 쓰는지 박제."""

    def _run_envelope(self, args):
        from click.testing import CliRunner

        from opencrab.cli import main

        runner = CliRunner()
        result = runner.invoke(main, args)
        # CliRunner는 stderr를 output에 합친다. info/warning 라인 뒤의 JSON 블록만 추출.
        out = result.output
        brace = out.index("{")
        envelope = json.loads(out[brace:])
        return result, envelope

    def test_cli_envelope_empty_store_shape(self, local_env):
        result, env = self._run_envelope(["query", "zzz no match here", "--json-envelope"])
        assert result.exit_code == 0
        # 빈 store → 결정적으로 빈 결과. envelope 키 집합 박제.
        assert set(env.keys()) == {
            "question",
            "spaces_filter",
            "pack_filter",
            "selected_packs",
            "total",
            "results",
        }
        assert env["question"] == "zzz no match here"
        assert env["spaces_filter"] is None
        assert env["total"] == 0
        assert env["results"] == []
        assert env["selected_packs"] == []
        assert env["pack_filter"] == {
            "pack_ids": None,
            "auto_pack": False,
            "include_unpackaged": False,
        }

    def test_cli_auto_pack_selects_and_emits_info(self, local_env):
        _write_pack_manifest(
            local_env,
            "nemotron-pack",
            title="Nemotron Pack",
            description="about nemotron",
            keywords=["nemotron"],
        )
        result, env = self._run_envelope(
            ["query", "tell me about nemotron", "--auto-pack", "--json-envelope"]
        )
        assert result.exit_code == 0
        assert "auto-pack selected 'nemotron-pack'" in result.output
        assert env["pack_filter"]["pack_ids"] == ["nemotron-pack"]
        assert env["pack_filter"]["auto_pack"] is True
        assert len(env["selected_packs"]) == 1
        assert env["selected_packs"][0]["pack_id"] == "nemotron-pack"

    def test_cli_pack_id_priority_warns_to_stderr(self, local_env):
        result, env = self._run_envelope(
            ["query", "q", "--pack-id", "pack-a", "--auto-pack", "--json-envelope"]
        )
        assert result.exit_code == 0
        assert "ignoring --auto-pack" in result.output
        assert env["pack_filter"]["pack_ids"] == ["pack-a"]
        assert env["pack_filter"]["auto_pack"] is False

    def test_cli_legacy_list_json_shape(self, local_env):
        """--json-output(envelope 아님)은 결과 리스트만 출력 (envelope dict 아님)."""
        from click.testing import CliRunner

        from opencrab.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["query", "zzz no match", "--json-output"])
        assert result.exit_code == 0
        out = result.output
        parsed = json.loads(out[out.index("[") :])
        assert parsed == []


class TestResolvePacksErrorPolicy:
    """공통 서비스 resolve_packs 의 예외 정책 분기 박제:
    MCP(raise_on_error=False) 는 graceful degrade(AUTO_PACK_FAILED 경고),
    CLI(raise_on_error=True) 는 예외 전파."""

    @staticmethod
    def _boom(*_a, **_k):
        raise RuntimeError("kaboom")

    def test_auto_pack_failure_graceful(self, monkeypatch):
        from opencrab.services.pack_selection import AUTO_PACK_FAILED, resolve_packs

        monkeypatch.setattr(
            "opencrab.ontology.pack_registry.load_pack_registry", self._boom
        )
        sel = resolve_packs("q", None, True, False, "/tmp", raise_on_error=False)
        assert sel.effective_pack_ids is None
        assert sel.selected_packs == []
        assert [w.code for w in sel.warnings] == [AUTO_PACK_FAILED]
        assert sel.warnings[0].detail == "kaboom"

    def test_auto_pack_failure_raises(self, monkeypatch):
        from opencrab.services.pack_selection import resolve_packs

        monkeypatch.setattr(
            "opencrab.ontology.pack_registry.load_pack_registry", self._boom
        )
        with pytest.raises(RuntimeError):
            resolve_packs("q", None, True, False, "/tmp", raise_on_error=True)

    def test_pack_ids_override_does_not_touch_registry(self, monkeypatch):
        # pack_ids 가 있으면 auto_pack 은 무력화되어 registry 를 건드리지 않는다
        # (override 경고만 — 예외 함수가 호출되면 안 됨).
        from opencrab.services.pack_selection import PACK_IDS_OVERRIDE_AUTO, resolve_packs

        monkeypatch.setattr(
            "opencrab.ontology.pack_registry.load_pack_registry", self._boom
        )
        sel = resolve_packs("q", ["pack-a"], True, False, "/tmp", raise_on_error=True)
        assert sel.effective_pack_ids == ["pack-a"]
        assert sel.auto_pack_active is False
        assert [w.code for w in sel.warnings] == [PACK_IDS_OVERRIDE_AUTO]


# ===========================================================================
# 2. Query response envelope (per-interface shape)
# ===========================================================================


class TestQueryResponseMCP:
    def test_envelope_full_shape(self):
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([_make_query_result("n1", "pack-a")])
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("alpha", pack_ids=["pack-a"])

        # MCP envelope 키 집합 (include_pack_provenance=True 기본).
        assert set(resp.keys()) == {
            "question",
            "spaces_filter",
            "subject_id",
            "tenant_id",
            "pipeline",
            "total",
            "results",
            "selected_packs",
            "pack_filter",
        }
        assert resp["question"] == "alpha"
        assert resp["spaces_filter"] is None
        assert resp["subject_id"] is None
        assert resp["tenant_id"] == "default"
        assert resp["pipeline"] == {"bm25": True, "rerank": True}
        assert resp["total"] == 1
        # 결과 항목은 QueryResult.to_dict() 형태.
        assert resp["results"][0]["node_id"] == "n1"
        assert resp["results"][0]["metadata"]["pack_id"] == "pack-a"
        # MCP 응답에는 keyword_fallback 키가 없다 (HTTP와의 차이).
        assert "keyword_fallback" not in resp

    def test_empty_results(self):
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([])
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("alpha")

        assert resp["total"] == 0
        assert resp["results"] == []

    def test_include_pack_provenance_false_drops_envelope_additions(self):
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([_make_query_result("n1", "pack-a")])
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("alpha", include_pack_provenance=False)

        assert "selected_packs" not in resp
        assert "pack_filter" not in resp
        # 레거시 핵심 키는 보존.
        for key in ("question", "spaces_filter", "subject_id", "tenant_id", "pipeline", "total", "results"):
            assert key in resp

    def test_limit_passed_through(self):
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([])
        with patch.object(tools, "_get_context", return_value=ctx):
            tools.ontology_query("alpha", limit=3)

        assert ctx["hybrid"].query.call_args.kwargs["limit"] == 3

    def test_spaces_filter_passed_through(self):
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([])
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("alpha", spaces=["claim", "policy"])

        assert resp["spaces_filter"] == ["claim", "policy"]
        assert ctx["hybrid"].query.call_args.kwargs["spaces"] == ["claim", "policy"]


class TestQueryResponseHTTP:
    def test_empty_store_shape(self, http_client):
        resp = http_client.post(
            "/api/query", json={"question": "zzz_no_match_keyword_zzz"}, headers=HTTP_AUTH
        )
        assert resp.status_code == 200
        body = resp.json()
        # HTTP query envelope 키 집합 — MCP와 다르다: keyword_fallback 있음,
        # selected_packs/pack_filter/subject_id/tenant_id/pipeline 없음.
        assert set(body.keys()) == {
            "question",
            "spaces_filter",
            "total",
            "results",
            "keyword_fallback",
        }
        assert body["question"] == "zzz_no_match_keyword_zzz"
        assert body["spaces_filter"] is None
        assert body["total"] == 0
        assert body["results"] == []
        assert body["keyword_fallback"] == []

    def test_query_requires_auth(self, http_client):
        resp = http_client.post("/api/query", json={"question": "x"})
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid API token."}

    def test_query_bad_token_rejected(self, http_client):
        resp = http_client.post(
            "/api/query", json={"question": "x"}, headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    def test_query_limit_validation_422(self, http_client):
        # QueryRequest.limit le=25 → 초과 시 pydantic 422.
        resp = http_client.post(
            "/api/query", json={"question": "x", "limit": 999}, headers=HTTP_AUTH
        )
        assert resp.status_code == 422

    def test_query_empty_question_422(self, http_client):
        resp = http_client.post("/api/query", json={"question": ""}, headers=HTTP_AUTH)
        assert resp.status_code == 422


# ===========================================================================
# 3. Node / edge write paths
#    MCP: builder 경유 (grammar 필수필드까지 검증, stores=neo4j/mongodb/postgres/chroma)
#    HTTP: 멀티스토어 직접 쓰기 (validate_node/edge만, stores=graph/documents/sql)
# ===========================================================================


class TestNodeEdgeWriteMCP:
    def test_add_node_success_shape(self, mcp_local_ctx):
        from opencrab.mcp import tools

        with patch.object(tools, "_get_context", return_value=mcp_local_ctx):
            # User는 grammar상 email/role 필수.
            result = tools.ontology_add_node(
                "subject", "User", "u1",
                {"name": "Alice", "email": "a@ex.com", "role": "admin"},
            )

        assert result["node_id"] == "u1"
        assert result["space"] == "subject"
        assert result["node_type"] == "User"
        # builder는 멀티스토어 결과를 neo4j/mongodb/postgres/chroma 키로 보고한다.
        assert set(result["stores"].keys()) == {"neo4j", "mongodb", "postgres", "chroma"}
        assert result["stores"]["neo4j"] == "ok"
        assert result["stores"]["postgres"] == "ok"
        assert result["stores"]["mongodb"].startswith("ok")
        assert result["stores"]["chroma"] == "ok"
        # receipt_id/receipt_ts 는 비결정적 — 존재/타입만.
        assert isinstance(result["receipt_id"], str)
        assert isinstance(result["receipt_ts"], str)

    def test_add_node_missing_required_field_is_error(self, mcp_local_ctx):
        """MCP builder는 grammar 필수 property 누락도 검증 → error dict (예외 아님)."""
        from opencrab.mcp import tools

        with patch.object(tools, "_get_context", return_value=mcp_local_ctx):
            result = tools.ontology_add_node("subject", "User", "u1", {"name": "Alice"})

        assert result["valid"] is False
        assert "email" in result["error"]
        assert "role" in result["error"]

    def test_add_node_invalid_space_is_error(self, mcp_local_ctx):
        from opencrab.mcp import tools

        with patch.object(tools, "_get_context", return_value=mcp_local_ctx):
            result = tools.ontology_add_node("badspace", "User", "x")

        assert result["valid"] is False
        assert "Unknown space 'badspace'" in result["error"]

    def test_add_node_duplicate_id_reupserts(self, mcp_local_ctx):
        """동일 node_id 재쓰기는 에러가 아니라 upsert (ok 응답)."""
        from opencrab.mcp import tools

        valid_props = {"name": "Alice", "email": "a@ex.com", "role": "admin"}
        with patch.object(tools, "_get_context", return_value=mcp_local_ctx):
            first = tools.ontology_add_node("subject", "User", "u1", valid_props)
            second = tools.ontology_add_node(
                "subject", "User", "u1", {**valid_props, "name": "Alice2"}
            )

        assert first["node_id"] == "u1"
        assert second["node_id"] == "u1"
        assert second["stores"]["postgres"] == "ok"
        assert second["properties"]["name"] == "Alice2"

    def test_add_edge_success_shape(self, mcp_local_ctx):
        from opencrab.mcp import tools

        valid_props = {"name": "Alice", "email": "a@ex.com", "role": "admin"}
        with patch.object(tools, "_get_context", return_value=mcp_local_ctx):
            tools.ontology_add_node("subject", "User", "u1", valid_props)
            tools.ontology_add_node("resource", "Project", "p1", {"name": "PX"})
            result = tools.ontology_add_edge("subject", "u1", "owns", "resource", "p1")

        assert result["from"] == {"space": "subject", "id": "u1"}
        assert result["relation"] == "owns"
        assert result["to"] == {"space": "resource", "id": "p1"}
        # edge builder는 node와 다른 store 키 셋을 보고: neo4j/postgres/mongodb(=audited).
        assert set(result["stores"].keys()) == {"neo4j", "postgres", "mongodb"}
        assert result["stores"]["neo4j"] == "ok"
        assert result["stores"]["postgres"] == "ok"
        assert result["stores"]["mongodb"] == "audited"
        assert isinstance(result["receipt_id"], str)

    def test_add_edge_invalid_relation_is_error(self, mcp_local_ctx):
        from opencrab.mcp import tools

        with patch.object(tools, "_get_context", return_value=mcp_local_ctx):
            result = tools.ontology_add_edge("subject", "u1", "mentions", "resource", "p1")

        assert result["valid"] is False
        assert "not valid" in result["error"]


class TestNodeEdgeWriteHTTP:
    def test_add_node_success_shape(self, http_client):
        resp = http_client.post(
            "/api/nodes",
            json={
                "space": "subject",
                "node_type": "User",
                "node_id": "u1",
                "properties": {"name": "Alice"},
            },
            headers=HTTP_AUTH,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["node_id"] == "u1"
        assert body["space"] == "subject"
        assert body["node_type"] == "User"
        # HTTP는 owner_id를 자동 주입 (X-User-Id).
        assert body["properties"]["owner_id"] == "tester"
        # HTTP stores 키는 graph/documents/sql (MCP의 neo4j/mongodb/postgres/chroma와 다름).
        assert set(body["stores"].keys()) == {"graph", "documents", "sql"}
        assert body["stores"]["graph"] == "ok"
        assert body["stores"]["documents"].startswith("ok")
        assert body["stores"]["sql"] == "ok"
        # HTTP는 grammar 필수 property(email/role)를 검증하지 않는다 — name만으로 200.
        # (MCP builder 경로와의 핵심 차이)

    def test_add_node_invalid_space_returns_500(self, http_client):
        """HTTP add_node는 validate_node().raise_if_invalid()의 ValueError를 잡지 않아 500.

        MCP는 동일 입력에 error dict(valid=False)를 반환한다 — 인터페이스 간 핵심 차이이며
        리팩토링에서 의도적으로 바꾸지 않는 한 보존되어야 하는 현재 동작.
        """
        resp = http_client.post(
            "/api/nodes",
            json={"space": "badspace", "node_type": "User", "node_id": "x"},
            headers=HTTP_AUTH,
        )
        assert resp.status_code == 500

    def test_add_node_missing_field_422(self, http_client):
        resp = http_client.post(
            "/api/nodes", json={"space": "subject"}, headers=HTTP_AUTH
        )
        assert resp.status_code == 422

    def test_add_node_duplicate_id_reupserts(self, http_client):
        payload = {
            "space": "subject",
            "node_type": "User",
            "node_id": "dup1",
            "properties": {"name": "A"},
        }
        first = http_client.post("/api/nodes", json=payload, headers=HTTP_AUTH)
        second = http_client.post(
            "/api/nodes",
            json={**payload, "properties": {"name": "B"}},
            headers=HTTP_AUTH,
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["stores"]["graph"] == "ok"

    def test_add_edge_success_shape(self, http_client):
        http_client.post(
            "/api/nodes",
            json={"space": "subject", "node_type": "User", "node_id": "u1", "properties": {"name": "A"}},
            headers=HTTP_AUTH,
        )
        http_client.post(
            "/api/nodes",
            json={"space": "resource", "node_type": "Project", "node_id": "p1", "properties": {"name": "PX"}},
            headers=HTTP_AUTH,
        )
        resp = http_client.post(
            "/api/edges",
            json={
                "from_space": "subject",
                "from_id": "u1",
                "relation": "owns",
                "to_space": "resource",
                "to_id": "p1",
            },
            headers=HTTP_AUTH,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["from"] == {"space": "subject", "id": "u1"}
        assert body["relation"] == "owns"
        assert body["to"] == {"space": "resource", "id": "p1"}
        # HTTP edge stores 키는 graph/sql (documents 없음 — MCP edge의 mongodb=audited와 다름).
        assert set(body["stores"].keys()) == {"graph", "sql"}
        assert body["stores"]["graph"] in {"ok", "no match"}
        assert body["stores"]["sql"] == "ok"

    def test_add_edge_invalid_relation_returns_500(self, http_client):
        """HTTP add_edge도 validate_edge().raise_if_invalid()의 ValueError를 잡지 않아 500."""
        resp = http_client.post(
            "/api/edges",
            json={
                "from_space": "subject",
                "from_id": "u1",
                "relation": "mentions",
                "to_space": "resource",
                "to_id": "p1",
            },
            headers=HTTP_AUTH,
        )
        assert resp.status_code == 500

    def test_node_write_requires_auth(self, http_client):
        resp = http_client.post(
            "/api/nodes",
            json={"space": "subject", "node_type": "User", "node_id": "u1"},
        )
        assert resp.status_code == 401
