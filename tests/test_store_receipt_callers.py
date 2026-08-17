"""이슈 #158 / #163 판정 게이트.

`OntologyBuilder.add_node`/`add_edge`(``opencrab/ontology/builder.py``)는 스토어별
쓰기 실패를 예외로 올리지 않고 ``result["stores"][store]`` 문자열로만 인코딩한다.
이 파일이 잠그는 회귀는 두 갈래다.

1. **#158**: 그 영수증을 소비해야 하는데 안 하는 세 호출자
   (``opencrab/cli.py`` extract, ``scripts/seed_ontology.py``,
   ``scripts/import_obsidian_vault.py``)가 "예외만 없으면 성공"으로 세는 결함.
   실패 영수증(모든 optional 스토어는 ``ok``, ``graph``만 실패)을 주입해도 세
   호출자가 여전히 성공으로 보고하면 여기서 걸린다.
2. **#163**: "이 쓰기가 요구하는 스토어 집합"을 판정하는 표준 API
   (``store_write_succeeded_for``, ``is_not_applicable_status``,
   ``REQUIRED_STORES``)가 없던 결함. 신규 API의 계약(양성 확인, fail-closed,
   미등록 kind 는 ValueError, 기존 병렬 billing 판정과의 동치)을 못박는다.

설계: ``/home/asdf/.claude/plans/localcrab-158-163-design.md`` C 절 "판정 게이트"
RED 표의 #1~#10 을 전량 구현한다.

**신규 API(``store_write_succeeded_for``/``is_not_applicable_status``/
``REQUIRED_STORES``)는 각 테스트 함수 본문 안에서만 import한다.** 소스 수정 전
(RED) 실행에서 파일 상단에 두면 전체 파일이 collection error 로 죽어 RED 증거가
무효가 된다 — 모듈 상단 import는 이미 존재하는 심볼만 쓴다.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from opencrab.cli import main
from opencrab.ontology.builder import graph_write_failed, store_write_succeeded

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# ---------------------------------------------------------------------------
# 공통 영수증 fixture (설계 C 서두 "전부 실패 fixture 공통 규약")
# ---------------------------------------------------------------------------
# optional 스토어(docs/sql)는 항상 ok — "아무 스토어나 ok 면 통과"하는 naive
# 구현으로는 이 fixture를 통과시킬 수 없게 만드는 비공허성 보강이다.
OK_RECEIPT = {"stores": {"graph": "ok", "docs": "ok (id=d1)", "sql": "ok"}}
FAIL_RECEIPT = {"stores": {"graph": "error: boom", "docs": "ok (id=d1)", "sql": "ok"}}
FAIL_RECEIPT_UNAVAILABLE = {
    "stores": {"graph": "unavailable", "docs": "ok (id=d1)", "sql": "ok"}
}
MALFORMED_RECEIPT = {"stores": "oops"}  # truthy non-dict — #6b


# ---------------------------------------------------------------------------
# 모듈 로더 (tests/test_common_utils_characterization.py 의 관례 재사용)
# ---------------------------------------------------------------------------


def _load_module_from_path(name: str, path: Path) -> types.ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot build spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


# ---------------------------------------------------------------------------
# cli.py extract — fixtures (tests/test_cli.py 의 cli_env/bootstrapped/runner 복제)
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    from opencrab.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.fixture()
def bootstrapped(cli_env):
    from opencrab.auth import bootstrap_local_user
    from opencrab.config import get_settings
    from opencrab.stores.factory import make_sql_store

    user_id, _secret = bootstrap_local_user(make_sql_store(get_settings()))
    return user_id


def _make_extraction_result_1n1e(source_id: str):
    from opencrab.ontology.extractor import ExtractedEdge, ExtractedNode, ExtractionResult

    return ExtractionResult(
        source_id=source_id,
        nodes=[
            ExtractedNode(
                space="subject", node_type="Agent", node_id="alex_agent",
                properties={"name": "Alex"},
            ),
        ],
        edges=[
            ExtractedEdge(
                from_space="subject", from_id="alex_agent", relation="owns",
                to_space="resource", to_id="demo_project", properties={},
            ),
        ],
        errors=[],
    )


def _invoke_extract(cli_env, runner, *, add_node, add_edge):
    """``add_node``/``add_edge``: a receipt dict to return, or an Exception
    instance to raise (for the #1b exception-path tests)."""
    (cli_env / "doc.md").write_text("Alex owns the demo project.")

    fake_extractor = MagicMock()
    fake_extractor.extract_from_file.side_effect = (
        lambda path: _make_extraction_result_1n1e(str(path))
    )

    fake_builder = MagicMock()
    if isinstance(add_node, Exception):
        fake_builder.add_node.side_effect = add_node
    else:
        fake_builder.add_node.return_value = add_node
    if isinstance(add_edge, Exception):
        fake_builder.add_edge.side_effect = add_edge
    else:
        fake_builder.add_edge.return_value = add_edge

    with patch(
        "opencrab.ontology.extractor.LLMExtractor", return_value=fake_extractor
    ), patch(
        "opencrab.ontology.builder.OntologyBuilder", return_value=fake_builder
    ):
        result = runner.invoke(
            main, ["extract", str(cli_env), "--api-key", "test-key"]
        )
    return result


# ---------------------------------------------------------------------------
# #1 계열 — cli.py extract, 영수증 경로 (양쪽/편측)
# ---------------------------------------------------------------------------


class TestCliExtractReceiptPath:
    def test_both_kinds_fail_reports_both_precisely(self, bootstrapped, cli_env, runner):
        """#1 — node/edge 둘 다 실패 영수증. 양쪽 문구를 정확히 단언한다(부분
        문자열 "1 not stored" 하나만으로는 반대쪽 배선 누락을 못 잡기 때문)."""
        result = _invoke_extract(cli_env, runner, add_node=FAIL_RECEIPT, add_edge=FAIL_RECEIPT)
        assert result.exit_code == 0, result.output
        assert "Done with store failures" in result.output
        assert "nodes=1 attempted (1 not stored)" in result.output
        assert "edges=1 attempted (1 not stored)" in result.output

    def test_node_only_failure_reports_asymmetric_counts(self, bootstrapped, cli_env, runner):
        """#1-node"""
        result = _invoke_extract(cli_env, runner, add_node=FAIL_RECEIPT, add_edge=OK_RECEIPT)
        assert result.exit_code == 0, result.output
        assert "nodes=1 attempted (1 not stored)" in result.output
        assert "edges=1 attempted (0 not stored)" in result.output

    def test_edge_only_failure_reports_asymmetric_counts(self, bootstrapped, cli_env, runner):
        """#1-edge — "unavailable" 변형 fixture 사용."""
        result = _invoke_extract(
            cli_env, runner, add_node=OK_RECEIPT, add_edge=FAIL_RECEIPT_UNAVAILABLE
        )
        assert result.exit_code == 0, result.output
        assert "nodes=1 attempted (0 not stored)" in result.output
        assert "edges=1 attempted (1 not stored)" in result.output

    def test_malformed_stores_counted_as_failure_not_success(self, bootstrapped, cli_env, runner):
        """#6b (cli 슬라이스) — stores 가 truthy non-dict("oops")여도 예외 없이
        실패로 계상되어야 한다."""
        result = _invoke_extract(cli_env, runner, add_node=MALFORMED_RECEIPT, add_edge=OK_RECEIPT)
        assert result.exit_code == 0, result.output
        assert "nodes=1 attempted (1 not stored)" in result.output
        assert "edges=1 attempted (0 not stored)" in result.output
        assert "Done with store failures" in result.output


# ---------------------------------------------------------------------------
# #1b 계열 — cli.py extract, 예외 경로 (node/edge 독립 except 블록)
# ---------------------------------------------------------------------------


class TestCliExtractExceptionPath:
    def test_add_node_exception_counted_as_node_failure_only(self, bootstrapped, cli_env, runner):
        """#1b-node — add_node 만 예외를 던진다. add_edge 는 별개의 except 블록이므로
        edge 쪽 실패 카운터가 (오배선으로) 같이 증가하면 안 된다."""
        result = _invoke_extract(
            cli_env, runner, add_node=RuntimeError("node boom"), add_edge=OK_RECEIPT
        )
        assert result.exit_code == 0, result.output
        assert "nodes=1 attempted (1 not stored)" in result.output
        assert "edges=1 attempted (0 not stored)" in result.output

    def test_add_edge_exception_counted_as_edge_failure_only(self, bootstrapped, cli_env, runner):
        """#1b-edge — 정반대 배선."""
        result = _invoke_extract(
            cli_env, runner, add_node=OK_RECEIPT, add_edge=RuntimeError("edge boom")
        )
        assert result.exit_code == 0, result.output
        assert "nodes=1 attempted (0 not stored)" in result.output
        assert "edges=1 attempted (1 not stored)" in result.output


# ---------------------------------------------------------------------------
# #2 — cli.py extract, 무실패 회귀 (수정 전후 동일해야 정상 = RED 에서도 PASS)
# ---------------------------------------------------------------------------


def test_extract_all_succeed_summary_line_unchanged(bootstrapped, cli_env, runner):
    """#2"""
    result = _invoke_extract(cli_env, runner, add_node=OK_RECEIPT, add_edge=OK_RECEIPT)
    assert result.exit_code == 0, result.output
    assert "nodes=1 edges=1 errors=0" in result.output
    assert "not stored" not in result.output
    assert "Done with store failures" not in result.output


# ---------------------------------------------------------------------------
# seed_ontology.py — helper
# ---------------------------------------------------------------------------


def _run_seed(monkeypatch, tmp_path, *, add_node_receipt, add_edge_receipt):
    """``seed_ontology.seed()`` 를 실 스토어 없이 완주시키고, 콘솔 출력 전문과
    NODES/EDGES 시드 데이터 개수를 반환한다."""
    import io

    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_MODE", "local")
    from opencrab.config import get_settings

    get_settings.cache_clear()

    seed_mod = _load_module_from_path(
        "_o163_seed_ontology", SCRIPTS_DIR / "seed_ontology.py"
    )

    buf = io.StringIO()
    seed_mod.console = Console(file=buf, no_color=True, width=200, force_terminal=False)

    fake_neo4j = MagicMock(available=True)
    fake_neo4j.count_nodes.return_value = 0
    fake_chroma = MagicMock(available=True)
    fake_chroma.count.return_value = 0
    fake_mongo = MagicMock(available=True)
    fake_mongo.collection_stats.return_value = {}
    fake_sql = MagicMock(available=True)
    fake_sql.table_counts.return_value = {}

    fake_builder = MagicMock()
    fake_builder.add_node.return_value = add_node_receipt
    fake_builder.add_edge.return_value = add_edge_receipt

    try:
        with patch(
            "opencrab.stores.factory.make_graph_store", return_value=fake_neo4j
        ), patch(
            "opencrab.stores.factory.make_vector_store", return_value=fake_chroma
        ), patch(
            "opencrab.stores.factory.make_doc_store", return_value=fake_mongo
        ), patch(
            "opencrab.stores.factory.make_sql_store", return_value=fake_sql
        ), patch(
            "opencrab.ontology.builder.OntologyBuilder", return_value=fake_builder
        ), patch(
            "opencrab.ontology.query.HybridQuery"
        ), patch(
            "opencrab.ontology.rebac.ReBACEngine"
        ):
            seed_mod.seed()
    finally:
        get_settings.cache_clear()

    return buf.getvalue(), len(seed_mod.NODES), len(seed_mod.EDGES)


# ---------------------------------------------------------------------------
# #3/#3s/#4/#4s/#6b — seed_ontology.py
# ---------------------------------------------------------------------------


class TestSeedOntology:
    def test_node_failures_reported_correctly(self, monkeypatch, tmp_path):
        """#3"""
        output, n_nodes, _n_edges = _run_seed(
            monkeypatch, tmp_path, add_node_receipt=FAIL_RECEIPT, add_edge_receipt=OK_RECEIPT
        )
        assert f"Nodes: 0 ok, {n_nodes} failed" in output

    def test_node_success_reported_correctly(self, monkeypatch, tmp_path):
        """#3s — 수정 전에도 PASS 여야 정상인 회귀 테스트."""
        output, n_nodes, _n_edges = _run_seed(
            monkeypatch, tmp_path, add_node_receipt=OK_RECEIPT, add_edge_receipt=OK_RECEIPT
        )
        assert f"Nodes: {n_nodes} ok, 0 failed" in output

    def test_edge_failures_reported_correctly(self, monkeypatch, tmp_path):
        """#4 — "unavailable" 변형 fixture 사용."""
        output, _n_nodes, n_edges = _run_seed(
            monkeypatch, tmp_path,
            add_node_receipt=OK_RECEIPT, add_edge_receipt=FAIL_RECEIPT_UNAVAILABLE,
        )
        assert f"Edges: 0 ok, {n_edges} failed" in output

    def test_edge_success_reported_correctly(self, monkeypatch, tmp_path):
        """#4s — 수정 전에도 PASS 여야 정상인 회귀 테스트."""
        output, _n_nodes, n_edges = _run_seed(
            monkeypatch, tmp_path, add_node_receipt=OK_RECEIPT, add_edge_receipt=OK_RECEIPT
        )
        assert f"Edges: {n_edges} ok, 0 failed" in output

    def test_malformed_stores_counted_as_failure(self, monkeypatch, tmp_path):
        """#6b (seed 슬라이스)"""
        output, n_nodes, _n_edges = _run_seed(
            monkeypatch, tmp_path, add_node_receipt=MALFORMED_RECEIPT, add_edge_receipt=OK_RECEIPT
        )
        assert f"Nodes: 0 ok, {n_nodes} failed" in output


# ---------------------------------------------------------------------------
# import_obsidian_vault.py — helper
# ---------------------------------------------------------------------------


def _run_import(tmp_path, monkeypatch, *, add_node_receipt, add_edge_receipt):
    """``_import_vault_unlocked`` 를 노트 1건(폴더/태그/위키링크 없음)으로 직접
    호출한다. 이 노트 하나로 add_node 3건(Document/TextUnit/Topic), add_edge 2건
    (contains/describes)이 정확히 나온다 — 폴더·태그·미해결링크 루프는 전부 0건."""
    note_path = tmp_path / "note.md"
    note_path.write_text("Just a plain note with no links or tags.", encoding="utf-8")

    import_mod = _load_module_from_path(
        "_o163_import_obsidian_vault", SCRIPTS_DIR / "import_obsidian_vault.py"
    )

    note = import_mod.build_note_record(tmp_path, note_path)
    assert note.folders == []
    assert note.tags == []
    assert note.wikilinks == []

    fake_builder = MagicMock()
    fake_builder.add_node.return_value = add_node_receipt
    fake_builder.add_edge.return_value = add_edge_receipt

    monkeypatch.setattr(import_mod, "OntologyBuilder", MagicMock(return_value=fake_builder))
    monkeypatch.setattr(import_mod, "Neo4jStore", MagicMock())
    monkeypatch.setattr(import_mod, "LocalDocStore", MagicMock())
    monkeypatch.setattr(import_mod, "SQLStore", MagicMock())

    return import_mod._import_vault_unlocked(
        vault_root=tmp_path,
        neo4j_uri="bolt://x", neo4j_user="u", neo4j_password="p", neo4j_database="d",
        local_data_dir=tmp_path,
        notes=[note],
    )


# ---------------------------------------------------------------------------
# #5/#5s/#6/#6s/#6b — import_obsidian_vault.py
# ---------------------------------------------------------------------------


class TestImportObsidianVault:
    def test_node_failures_yield_zero_confirmed_writes(self, tmp_path, monkeypatch):
        """#5 — ``.get(..., 0)``으로 아직 없는 실패-카운터 키를 방어한다: 수정
        전에는 그 키 자체가 없으므로 없으면 0으로 읽어 이 보조 단언은 RED에서
        조용히 통과하고, 주 단언(nodes_written == 0)이 RED 실패를 담당한다."""
        result = _run_import(
            tmp_path, monkeypatch, add_node_receipt=FAIL_RECEIPT, add_edge_receipt=OK_RECEIPT
        )
        assert result["nodes_written"] == 0
        assert result.get("node_write_failures", 0) == 3

    def test_node_success_all_confirmed(self, tmp_path, monkeypatch):
        """#5s — 수정 전에도 PASS 여야 정상인 회귀 테스트."""
        result = _run_import(
            tmp_path, monkeypatch, add_node_receipt=OK_RECEIPT, add_edge_receipt=OK_RECEIPT
        )
        assert result["nodes_written"] == 3
        assert result.get("node_write_failures", 0) == 0

    def test_edge_failures_yield_zero_confirmed_writes(self, tmp_path, monkeypatch):
        """#6 — "unavailable" 변형 fixture 사용."""
        result = _run_import(
            tmp_path, monkeypatch,
            add_node_receipt=OK_RECEIPT, add_edge_receipt=FAIL_RECEIPT_UNAVAILABLE,
        )
        assert result["edges_written"] == 0
        assert result.get("edge_write_failures", 0) == 2

    def test_edge_success_all_confirmed(self, tmp_path, monkeypatch):
        """#6s — 수정 전에도 PASS 여야 정상인 회귀 테스트."""
        result = _run_import(
            tmp_path, monkeypatch, add_node_receipt=OK_RECEIPT, add_edge_receipt=OK_RECEIPT
        )
        assert result["edges_written"] == 2
        assert result.get("edge_write_failures", 0) == 0

    def test_malformed_stores_counted_as_failure(self, tmp_path, monkeypatch):
        """#6b (import 슬라이스)"""
        result = _run_import(
            tmp_path, monkeypatch, add_node_receipt=MALFORMED_RECEIPT, add_edge_receipt=OK_RECEIPT
        )
        assert result["nodes_written"] == 0
        assert result.get("node_write_failures", 0) == 3


# ---------------------------------------------------------------------------
# #7 — store_write_succeeded_for 계약 (신규 API — 함수 본문 안에서 import)
# ---------------------------------------------------------------------------


def test_store_write_succeeded_for_contract():
    """#7"""
    from opencrab.ontology.builder import store_write_succeeded_for

    assert store_write_succeeded_for({"graph": "ok"}, "node") is True
    assert store_write_succeeded_for({"graph": "ok (id=1)"}, "node") is True
    assert store_write_succeeded_for({"graph": "audited"}, "node") is False
    assert store_write_succeeded_for({"graph": "unavailable"}, "node") is False
    assert store_write_succeeded_for(
        {"graph": "error: x", "docs": "ok", "sql": "ok"}, "node"
    ) is False  # optional 만 ok 여도 False
    assert store_write_succeeded_for({"docs": "ok"}, "node") is False  # graph 부재
    assert store_write_succeeded_for({"chromadb": "ok (id=v1)"}, "chunk") is True
    assert store_write_succeeded_for({"chromadb": "ok (id=v1)"}, "node") is False

    for bad_stores in ("not a dict", None, [], 42):
        assert store_write_succeeded_for(bad_stores, "node") is False  # 무예외

    for bad_kind in ("chunks", "", None, ["node"], {"a": 1}):
        with pytest.raises(ValueError):
            store_write_succeeded_for({"graph": "ok"}, bad_kind)


def test_store_write_succeeded_for_empty_required_set_fails_closed(monkeypatch):
    """#7 — 빈 required 집합은 오늘 도달 불가지만 fail-closed 를 테스트로 못박는다.
    monkeypatch.setitem 은 테스트 종료 시 자동 복구되므로 다른 테스트로 안 샌다."""
    from opencrab.ontology.builder import REQUIRED_STORES, store_write_succeeded_for

    monkeypatch.setitem(REQUIRED_STORES, "node", frozenset())
    assert store_write_succeeded_for({"graph": "ok"}, "node") is False


# ---------------------------------------------------------------------------
# #8 — is_not_applicable_status 계약
# ---------------------------------------------------------------------------


def test_is_not_applicable_status_contract():
    """#8"""
    from opencrab.ontology.builder import is_not_applicable_status

    assert is_not_applicable_status("audited") is True
    for status in ("ok", "unavailable", "error: x", "skipped (no text)", None, 200, {}):
        assert is_not_applicable_status(status) is False


# ---------------------------------------------------------------------------
# #9 — 병렬 billing 정책과의 동치 (조합 생성, 표본 나열이 아니라 곱집합)
# ---------------------------------------------------------------------------


def test_parallel_billing_policy_equivalence_combinatorial():
    """#9 — store_write_succeeded_for(s,"node") == store_write_succeeded_for(s,"edge")
    == store_write_succeeded(s,"graph") == (not graph_write_failed(s)) 를 161개
    입력 전량에서 단언한다. 이 동치는 "간접 방지"가 아니라 설계 A절의 명시 계약이다
    (기존 billing 호출자 4곳은 이관하지 않고 병렬 존속시키는 것이 이슈 #163의
    요건이므로, 두 경로가 갈라지면 여기서 잡아야 한다)."""
    from opencrab.ontology.builder import store_write_succeeded_for

    statuses: list[Any] = [
        "ok", "ok (id=1)", "okay", "ok-error: x", "unavailable", "error: boom",
        "no match", "no match (missing node: a)", "audited", "skipped (no text)",
        None, 200,
    ]

    cases: list[Any] = []
    for g in statuses:
        for d in statuses:
            cases.append({"graph": g, "docs": d})
    for d in statuses:
        cases.append({"docs": d})  # graph 키 부재
    cases.extend(["not a dict", None, [], 42])  # 비-dict 4종
    cases.append({})

    assert len(cases) == 12 * 12 + 12 + 4 + 1 == 161

    for stores in cases:
        node = store_write_succeeded_for(stores, "node")
        edge = store_write_succeeded_for(stores, "edge")
        graph_ok = store_write_succeeded(stores, "graph")
        not_failed = not graph_write_failed(stores)
        assert node == edge == graph_ok == not_failed, stores


# ---------------------------------------------------------------------------
# #10 — REQUIRED_STORES pin
# ---------------------------------------------------------------------------


def test_required_stores_pin():
    """#10 — 이 값을 바꾸려면 하드코딩된 billing 판정 4곳
    (opencrab/mcp/tools/graph.py, opencrab/mcp/tools/harness.py,
    opencrab/mcp/tools/pack.py, crabharness/crabharness/apply.py)도 같이
    검토해야 한다. (b)(조합 동치, #9)가 안 깨지는 변경(예: chunk 매핑 변경)도
    이 pin 테스트는 잡는다."""
    from opencrab.ontology.builder import REQUIRED_STORES

    assert REQUIRED_STORES == {
        "node": frozenset({"graph"}),
        "edge": frozenset({"graph"}),
        "chunk": frozenset({"chromadb"}),
    }
