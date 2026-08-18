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

from opencrab.ontology.query import QueryOutcome, QueryResult

REPO_ROOT = Path(__file__).resolve().parents[1]

# #145: ontology_query()/ontology_add_node()/ontology_add_edge() now call
# current_principal() internally; bind a fixed test principal for every test
# in this module (see conftest.py's bind_test_principal). TestNodeEdgeWriteMCP
# via mcp_local_ctx and TestNodeEdgeWriteHTTP via http_auth bind their OWN
# real bootstrapped principal instead (nested principal_scope wins for their
# duration), since those paths characterize real audit/owner attribution.
pytestmark = pytest.mark.usefixtures("bind_test_principal")


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

    # 기본 EMBEDDING_BACKEND="openai" 이면 vector store(sqlite-vec/chroma)가 실제
    # KURE EF(OpenAI 원격 서버 + GGUF 자동 다운로드 폴백)를 만든다. 이 파일의 CLI
    # query 경로(TestPackSelectionCLI)와 node/edge 쓰기 경로(TestNodeEdgeWriteMCP)가
    # 모두 실제 vector store를 거치므로, factory 레벨에서 목으로 대체해 CI에서
    # 네트워크/635MB 모델 다운로드 의존을 없앤다 (박제 대상은 store 반환 dict
    # 형태이지 임베딩 품질이 아니다).
    from opencrab.stores import factory

    monkeypatch.setattr(factory, "_make_kure_embedding_function", lambda settings: _MockKureEF())

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
def local_principal(local_stores):
    """Bootstrap a real local user+token against local_stores["sql"] (#145:
    every write path now needs a real server-derived principal -- there is
    no more X-User-Id / shared-key stand-in). Returns (user_id, secret)."""
    from opencrab.auth import bootstrap_local_user

    return bootstrap_local_user(local_stores["sql"])


@pytest.fixture()
def mcp_local_ctx(local_stores, builder, local_principal):
    """MCP _get_context()가 반환하는 형태의 실 로컬 ctx (빌더 경유 쓰기 박제용).

    #145: ontology_add_node/ontology_add_edge now call current_principal()
    internally -- bind local_principal's bootstrapped user for the whole
    fixture lifetime so every test using this ctx has one, without each
    test body having to open its own principal_scope.
    """
    from opencrab.auth import Principal, principal_scope
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
    ctx = {
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
    user_id, _secret = local_principal
    with principal_scope(Principal(user_id=user_id, is_local=True, disabled=False)):
        yield ctx


class _MockKureEF:
    """KURE 임베딩 EF 목(mock) — 네트워크 호출/GGUF 자동 다운로드 없이 결정적
    고정 차원 벡터를 반환한다. tests/test_resilient_embedding.py 의 _MockEF /
    _MockFallback 패턴과 동일한 최소 프로토콜(callable + name())만 구현한다.

    node/edge 쓰기 경로 박제는 실제 vector store(SqliteVecStore/ChromaStore)의
    반환 dict 형태가 목적이지 임베딩 품질이 아니므로, factory._make_kure_embedding_function
    을 이걸로 대체해 ResilientEmbeddingFunction(OpenAI 원격 + GGUF 폴백) 의존을 없앤다.
    """

    def __init__(self, dim: int = 1024) -> None:
        self._dim = dim

    def __call__(self, input: list[str]) -> list[list[float]]:
        vec = [1.0] + [0.0] * (self._dim - 1)
        return [vec for _ in input]

    def name(self) -> str:
        return "kure_v1"


def _make_query_result(node_id: str = "n1", pack_id: str | None = "pack-a") -> QueryResult:
    meta: dict = {"node_id": node_id}
    if pack_id:
        meta["pack_id"] = pack_id
    return QueryResult(source="vector", node_id=node_id, score=0.9, text="alpha", metadata=meta)


def _mock_query_ctx(results, sql=None):
    """ontology_query envelope 분기 박제용: hybrid만 MagicMock, 나머지는 stub.

    #51: HybridQuery.query()는 QueryOutcome(results, warnings)을 반환한다
    (더 이상 bare list가 아님) — 실제 계약과 맞춰 mock한다.

    #147: ontology_query now derives its read scope from
    ``ctx["sql"]`` + ``current_principal()`` (opencrab.mcp.tools._current_read_scope
    -> opencrab.pack.read_scope.read_scope -> ownership.readable_pack_ids),
    which issues a real SQL query. A bare MagicMock() here would either
    TypeError or silently answer "everything readable" via mock attribute
    access, which is exactly the fail-open this execution closes -- so
    ``ctx["sql"]`` must be a real (in-memory) SQLStore. Callers that need the
    bound test principal ("test-user", see conftest.py's bind_test_principal)
    to own a pack pass their own ``sql`` (already populated via
    ``opencrab.pack.ownership.create_pack``) instead of relying on the default.
    """
    if sql is None:
        from opencrab.stores.sql_store import SQLStore

        sql = SQLStore("sqlite:///:memory:")
    hybrid = MagicMock()
    hybrid.query.return_value = QueryOutcome(results=results, warnings=[])
    billing = MagicMock()
    billing.on_query = MagicMock()
    return {
        "neo4j": MagicMock(),
        "chroma": MagicMock(),
        "mongo": MagicMock(),
        "sql": sql,
        "builder": MagicMock(),
        "rebac": MagicMock(),
        "impact": MagicMock(),
        "hybrid": hybrid,
        "billing": billing,
    }


def _owned_sql(*pack_ids):
    """Real in-memory SQLStore with each pack_id owned by "test-user" (the
    fixed principal ``bind_test_principal`` binds for this whole module) --
    so it lands inside ``read_scope``'s output and this module's MCP-level
    pack-selection tests keep exercising the SAME scenario they always did
    (an owned pack a query can actually resolve/select), not an accidentally-
    always-empty scope."""
    from opencrab.pack.ownership import create_pack
    from opencrab.stores.sql_store import SQLStore

    sql = SQLStore("sqlite:///:memory:")
    for pack_id in pack_ids:
        create_pack(sql, "test-user", pack_id)
    return sql


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
    """TestClient with lifespan entered. raise_server_exceptions=False로 500도 응답으로 관찰.

    #145: OPENCRAB_API_KEY no longer configures anything -- setting it would
    now trip refuse_stale_shared_secret_env() and fail app startup outright.
    Auth is per-user bearer tokens (see the http_auth fixture below), bootstrapped
    against the same LOCAL_DATA_DIR the app's own lifespan-built SQLStore opens.
    """
    monkeypatch.setenv("OPENCRAB_TIER", "free")
    from fastapi.testclient import TestClient

    with TestClient(api_module.app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture()
def http_auth(local_principal):
    """Bearer-auth headers for a real bootstrapped local user+token (#145:
    replaces the deleted OPENCRAB_API_KEY + X-User-Id combination -- the
    principal is now server-derived from this token, never client-asserted)."""
    _user_id, secret = local_principal
    return {"Authorization": f"Bearer {secret}"}


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

        # #147: nemotron-pack must be owned by the bound principal ("test-user")
        # or auto_pack's candidate list is filtered to empty BEFORE scoring
        # (resolve_packs filters the disk registry to `p.pack_id in scope`
        # before choose_packs runs) regardless of how well the query matches.
        ctx = _mock_query_ctx(
            [_make_query_result(pack_id="nemotron-pack")], sql=_owned_sql("nemotron-pack")
        )
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

        # #147: pack-a must be in the caller's read scope, or resolve_packs's
        # narrow() drops it and the result is PACK_IDS_OUT_OF_SCOPE instead of
        # the override-auto-pack warning this test pins.
        ctx = _mock_query_ctx([_make_query_result(pack_id="pack-a")], sql=_owned_sql("pack-a"))
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

        # #147: own nemotron-pack so it survives resolve_packs's scope filter
        # and reaches choose_packs -- this test's whole point is that the
        # KEYWORD MATCH is what falls short, not the scope filter.
        ctx = _mock_query_ctx([], sql=_owned_sql("nemotron-pack"))
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("totally unrelated random words", auto_pack=True)

        assert resp["selected_packs"] == []
        # #147: effective_pack_ids is never None -- "no explicit pack_ids"
        # resolves to the caller's whole readable scope, which here is
        # exactly the one owned pack (auto_pack couldn't select FROM it, but
        # the fallback filter is still "everything you may read", not "no
        # filter").
        assert resp["pack_filter"]["pack_ids"] == ["nemotron-pack"]
        assert resp["pack_filter"]["auto_pack"] is True
        # #147: wording changed -- there is no more "full-store search" state
        # to fall back to; the fallback is the caller's own readable scope.
        assert resp["pack_filter"]["warnings"] == [
            "auto_pack could not select a pack above the score threshold; "
            "searching all packs you can read"
        ]

    def test_auto_pack_no_registry_falls_back(self, local_env):
        """packs 디렉토리가 없으면 (registry 비어 있음) 임계값 미달과 동일하게 fallback."""
        from opencrab.mcp import tools

        # No pack owned either -- scope is empty, same as an empty disk registry.
        ctx = _mock_query_ctx([], sql=_owned_sql())
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("anything", auto_pack=True)

        assert resp["selected_packs"] == []
        # #147: never None -- an empty readable scope is a concrete empty list.
        assert resp["pack_filter"]["pack_ids"] == []
        assert "warnings" in resp["pack_filter"]

    def test_include_unpackaged_without_pack_filter_warns(self, local_env):
        from opencrab.mcp import tools

        ctx = _mock_query_ctx([], sql=_owned_sql())
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("q", include_unpackaged=True)

        # #147: the echoed value is PackSelection.include_unpackaged_effective,
        # which is hardcoded False everywhere (DESIGN.md §3.2) -- it reports
        # what was actually honoured, not the caller's raw request. Echoing
        # the caller's input back as "True" here would tell them the flag
        # took effect when it categorically never does.
        assert resp["pack_filter"]["include_unpackaged"] is False
        # #147: wording changed -- include_unpackaged is unconditionally
        # unhonoured now (reads are always pack-scoped), not merely "no
        # effect without pack_ids/auto_pack".
        assert resp["pack_filter"]["warnings"] == [
            "include_unpackaged is not honoured: reads are always scoped to "
            "the packs you can read"
        ]

    # #147 INTENTIONALLY FLIPPED PIN (see DESIGN.md §3.2, listed in the PR
    # body's flipped-pin list): INCLUDE_UNPACKAGED_NOOP used to fire only
    # when "include_unpackaged and not effective" (no pack filter active).
    # opencrab/services/pack_selection.py::resolve_packs now fires it
    # whenever `include_unpackaged` is truthy, full stop -- because leaving
    # it conditional on pack_ids would tell a caller who explicitly asked for
    # unpackaged rows AND named packs that their request had no effect "for
    # lack of pack_ids", which is false. The scenario this test's old name
    # promised ("no warning when pack_ids is given") no longer exists.
    # HybridQuery.query() also no longer takes an include_unpackaged
    # parameter at all (DESIGN.md §3.3) -- ontology_query() never forwards
    # it -- so the old `ctx["hybrid"].query.call_args.kwargs["include_unpackaged"]
    # is True` assertion pins a kwarg that is never sent anymore; replaced
    # with a lock on its absence.
    def test_include_unpackaged_with_pack_ids_still_warns_and_is_not_forwarded(
        self, local_env
    ):
        from opencrab.mcp import tools

        ctx = _mock_query_ctx(
            [_make_query_result(pack_id="pack-a")], sql=_owned_sql("pack-a")
        )
        with patch.object(tools, "_get_context", return_value=ctx):
            resp = tools.ontology_query("q", pack_ids=["pack-a"], include_unpackaged=True)

        # #147: always the effective (hardcoded False) value, not the
        # caller's raw request -- see the sibling test above.
        assert resp["pack_filter"]["include_unpackaged"] is False
        assert resp["pack_filter"]["warnings"] == [
            "include_unpackaged is not honoured: reads are always scoped to "
            "the packs you can read"
        ]
        assert "include_unpackaged" not in ctx["hybrid"].query.call_args.kwargs


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

    def _bootstrap_cli_user(self, *own_pack_ids):
        """#147: CLI `query` now calls require_local_principal() first and
        derives its pack filter from that principal's readable scope -- a
        command that used to run with no principal bound at all now needs
        one bootstrapped first, and (for tests that name --pack-id/expect
        auto_pack to find a pack) that principal must own the pack_ids the
        test cares about, or resolve_packs's scope-intersection drops them."""
        from opencrab.auth import bootstrap_local_user
        from opencrab.config import get_settings
        from opencrab.pack.ownership import create_pack
        from opencrab.stores.factory import make_sql_store

        sql = make_sql_store(get_settings())
        user_id, _secret = bootstrap_local_user(sql)
        for pack_id in own_pack_ids:
            create_pack(sql, user_id, pack_id)
        return user_id

    def test_cli_envelope_empty_store_shape(self, local_env):
        self._bootstrap_cli_user()
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
        # #147: effective_pack_ids is never None -- an owner of no packs has
        # a concrete, empty readable scope, not "no filter".
        assert env["pack_filter"] == {
            "pack_ids": [],
            "auto_pack": False,
            "include_unpackaged": False,
        }

    def test_cli_auto_pack_selects_and_emits_info(self, local_env):
        self._bootstrap_cli_user("nemotron-pack")
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
        self._bootstrap_cli_user("pack-a")
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

        self._bootstrap_cli_user()
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
        sel = resolve_packs(
            "q", None, True, False, "/tmp", scope=frozenset(), raise_on_error=False
        )
        # #147: effective_pack_ids is never None -- no requested pack_ids
        # resolves to the caller's (here empty) readable scope, a concrete list.
        assert sel.effective_pack_ids == []
        assert sel.selected_packs == []
        assert [w.code for w in sel.warnings] == [AUTO_PACK_FAILED]
        assert sel.warnings[0].detail == "kaboom"

    def test_auto_pack_failure_raises(self, monkeypatch):
        from opencrab.services.pack_selection import resolve_packs

        monkeypatch.setattr(
            "opencrab.ontology.pack_registry.load_pack_registry", self._boom
        )
        with pytest.raises(RuntimeError):
            resolve_packs(
                "q", None, True, False, "/tmp", scope=frozenset(), raise_on_error=True
            )

    def test_pack_ids_override_does_not_touch_registry(self, monkeypatch):
        # pack_ids 가 있으면 auto_pack 은 무력화되어 registry 를 건드리지 않는다
        # (override 경고만 — 예외 함수가 호출되면 안 됨).
        from opencrab.services.pack_selection import PACK_IDS_OVERRIDE_AUTO, resolve_packs

        monkeypatch.setattr(
            "opencrab.ontology.pack_registry.load_pack_registry", self._boom
        )
        # scope must include "pack-a" or narrow() drops it and adds an extra
        # PACK_IDS_OUT_OF_SCOPE warning this test isn't about.
        sel = resolve_packs(
            "q", ["pack-a"], True, False, "/tmp",
            scope=frozenset({"pack-a"}), raise_on_error=True,
        )
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
        # #145: subject_id is the bind_test_principal fixture's principal
        # (no longer a client-supplied argument, and never None -- a
        # principal is always bound by the time a handler runs).
        assert resp["subject_id"] == "test-user"
        assert resp["tenant_id"] == "default"
        assert resp["pipeline"] == {"bm25": True, "rerank": True, "fts": True}
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
    def test_empty_store_shape(self, http_client, http_auth):
        resp = http_client.post(
            "/api/query", json={"question": "zzz_no_match_keyword_zzz"}, headers=http_auth
        )
        assert resp.status_code == 200
        body = resp.json()
        # §1.4 수렴: HTTP envelope 도 이제 selected_packs/pack_filter 를 포함한다
        # (superset). keyword_fallback 은 HTTP 고유로 유지, subject_id/tenant_id/
        # pipeline 은 여전히 MCP 전용.
        assert set(body.keys()) == {
            "question",
            "spaces_filter",
            "total",
            "results",
            "keyword_fallback",
            "selected_packs",
            "pack_filter",
        }
        assert body["question"] == "zzz_no_match_keyword_zzz"
        assert body["spaces_filter"] is None
        assert body["total"] == 0
        assert body["results"] == []
        assert body["keyword_fallback"] == []
        assert body["selected_packs"] == []
        # #147: effective_pack_ids is never None -- the http_auth principal
        # (a freshly bootstrapped local user, see local_principal fixture)
        # owns no packs, so its readable scope is a concrete empty list.
        assert body["pack_filter"] == {
            "pack_ids": [],
            "auto_pack": False,
            "include_unpackaged": False,
        }

    def test_query_requires_auth(self, http_client):
        resp = http_client.post("/api/query", json={"question": "x"})
        assert resp.status_code == 401
        # #145: require_auth's message changed with the shared-key -> bearer-
        # token switch (see apps/api/main.py's require_auth).
        assert resp.json() == {"detail": "Invalid or missing bearer token."}

    def test_query_bad_token_rejected(self, http_client):
        resp = http_client.post(
            "/api/query", json={"question": "x"}, headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    def test_query_limit_validation_422(self, http_client, http_auth):
        # QueryRequest.limit le=25 → 초과 시 pydantic 422.
        resp = http_client.post(
            "/api/query", json={"question": "x", "limit": 999}, headers=http_auth
        )
        assert resp.status_code == 422

    def test_query_empty_question_422(self, http_client, http_auth):
        resp = http_client.post("/api/query", json={"question": ""}, headers=http_auth)
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
        # §1.3 수렴: builder 는 멀티스토어 결과를 역할 기반 키(graph/docs/sql/vector)로
        # 보고한다(이전의 백엔드 제품명 neo4j/mongodb/postgres/chroma 대신).
        assert set(result["stores"].keys()) == {"graph", "docs", "sql", "vector"}
        assert result["stores"]["graph"] == "ok"
        assert result["stores"]["sql"] == "ok"
        assert result["stores"]["docs"].startswith("ok")
        assert result["stores"]["vector"] == "ok"
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
        assert second["stores"]["sql"] == "ok"
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
        # §1.3 수렴: edge builder 도 역할 기반 키(graph/sql/docs). docs 는 audit 표식.
        assert set(result["stores"].keys()) == {"graph", "sql", "docs"}
        assert result["stores"]["graph"] == "ok"
        assert result["stores"]["sql"] == "ok"
        assert result["stores"]["docs"] == "audited"
        assert isinstance(result["receipt_id"], str)

    def test_add_edge_invalid_relation_is_error(self, mcp_local_ctx):
        from opencrab.mcp import tools

        with patch.object(tools, "_get_context", return_value=mcp_local_ctx):
            result = tools.ontology_add_edge("subject", "u1", "mentions", "resource", "p1")

        assert result["valid"] is False
        assert "not valid" in result["error"]


class TestNodeEdgeWriteHTTP:
    def test_add_node_success_shape(self, http_client, http_auth, local_principal):
        resp = http_client.post(
            "/api/nodes",
            json={
                "space": "subject",
                "node_type": "User",
                "node_id": "u1",
                # §1.2 수렴: HTTP 도 이제 builder 경유라 User 필수필드(email/role)를 요구.
                "properties": {"name": "Alice", "email": "a@ex.com", "role": "admin"},
            },
            headers=http_auth,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["node_id"] == "u1"
        assert body["space"] == "subject"
        assert body["node_type"] == "User"
        # #145 inversion: owner_id used to come straight from the
        # client-asserted X-User-Id header ("tester", trusted with no
        # verification -- #143's #72). It is now the verified bearer
        # token's own principal -- a client can no longer spoof it by
        # sending a different header value (see test_add_node below has no
        # X-User-Id equivalent to send at all anymore).
        expected_user_id, _secret = local_principal
        assert body["properties"]["owner_id"] == expected_user_id
        # §1.3 수렴: HTTP/MCP 모두 역할 기반 stores 키(graph/docs/sql/vector).
        assert set(body["stores"].keys()) == {"graph", "docs", "sql", "vector"}
        assert body["stores"]["graph"] == "ok"
        assert body["stores"]["docs"].startswith("ok")
        assert body["stores"]["sql"] == "ok"
        # §1.6 수렴: HTTP 응답에도 receipt 가 생긴다.
        assert isinstance(body["receipt_id"], str)
        assert isinstance(body["receipt_ts"], str)

    def test_add_node_invalid_space_returns_422(self, http_client, http_auth):
        """§1.1 수렴: grammar 검증 실패는 이제 명시적 422(이전엔 미포착 ValueError→500).

        MCP 는 동일 입력에 error dict(valid=False)를 반환한다 — 전송 계층별 표현은
        다르되(REST 4xx vs MCP dict) 둘 다 '클라이언트 오류'로 명확히 처리한다.
        """
        resp = http_client.post(
            "/api/nodes",
            json={"space": "badspace", "node_type": "User", "node_id": "x"},
            headers=http_auth,
        )
        assert resp.status_code == 422
        assert "badspace" in resp.json()["detail"]

    def test_add_node_missing_grammar_field_returns_422(self, http_client, http_auth):
        """§1.2 수렴: HTTP 도 grammar 필수 property(email/role) 누락 시 422.

        이전에는 HTTP 가 필수필드를 검증하지 않아 name 만으로 200 통과했다. builder
        경유로 통일되어 검증이 강화되었다(제약 강화 — 기존 데이터는 소급 거부되지 않음)."""
        resp = http_client.post(
            "/api/nodes",
            json={
                "space": "subject",
                "node_type": "User",
                "node_id": "u_incomplete",
                "properties": {"name": "Alice"},
            },
            headers=http_auth,
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "email" in detail and "role" in detail

    def test_add_node_missing_field_422(self, http_client, http_auth):
        resp = http_client.post(
            "/api/nodes", json={"space": "subject"}, headers=http_auth
        )
        assert resp.status_code == 422

    def test_add_node_duplicate_id_reupserts(self, http_client, http_auth):
        payload = {
            "space": "subject",
            "node_type": "User",
            "node_id": "dup1",
            "properties": {"name": "A", "email": "a@ex.com", "role": "admin"},
        }
        first = http_client.post("/api/nodes", json=payload, headers=http_auth)
        second = http_client.post(
            "/api/nodes",
            json={**payload, "properties": {**payload["properties"], "name": "B"}},
            headers=http_auth,
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["stores"]["graph"] == "ok"

    def test_add_edge_success_shape(self, http_client, http_auth):
        http_client.post(
            "/api/nodes",
            json={"space": "subject", "node_type": "User", "node_id": "u1", "properties": {"name": "A", "email": "a@ex.com", "role": "admin"}},
            headers=http_auth,
        )
        http_client.post(
            "/api/nodes",
            json={"space": "resource", "node_type": "Project", "node_id": "p1", "properties": {"name": "PX"}},
            headers=http_auth,
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
            headers=http_auth,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["from"] == {"space": "subject", "id": "u1"}
        assert body["relation"] == "owns"
        assert body["to"] == {"space": "resource", "id": "p1"}
        # §1.3 수렴: HTTP edge 도 역할 기반 키(graph/sql/docs). docs 는 audit 표식.
        assert set(body["stores"].keys()) == {"graph", "sql", "docs"}
        assert body["stores"]["graph"] in {"ok", "no match"}
        assert body["stores"]["sql"] == "ok"
        assert body["stores"]["docs"] == "audited"
        # §1.6 수렴: edge 응답에도 receipt.
        assert isinstance(body["receipt_id"], str)

    def test_add_edge_invalid_relation_returns_422(self, http_client, http_auth):
        """§1.1 수렴: 잘못된 relation 은 이제 명시적 422(이전엔 미포착 ValueError→500)."""
        resp = http_client.post(
            "/api/edges",
            json={
                "from_space": "subject",
                "from_id": "u1",
                "relation": "mentions",
                "to_space": "resource",
                "to_id": "p1",
            },
            headers=http_auth,
        )
        assert resp.status_code == 422

    def test_node_write_requires_auth(self, http_client):
        resp = http_client.post(
            "/api/nodes",
            json={"space": "subject", "node_type": "User", "node_id": "u1"},
        )
        assert resp.status_code == 401
