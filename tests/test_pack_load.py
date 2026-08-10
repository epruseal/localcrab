"""``opencrab.pack.load`` — 적재 계층 계약과 행동.

이 모듈은 팩 파일(`{nodes,edges,chunks}.jsonl`)을 4스토어에 반영한다. 생산자와 소비자가
다른 리포에 있던 동안 아무도 둘을 대조하지 않았고, 그래서 노드 커스텀 필드 91만 건이
파일에는 있는데 라이브에는 없는 채로 방치됐다. 그 사고를 첫 실행에서 잡았을 검사가
`TestCustomPropertiesSurvive` 다.

스토어는 **진짜 SQLite 3종**을 tmp_path 에 만든다(fake 아님). 외부 의존이 0이라 CI 에서
항상 돌면서 실제 SQL 동작을 검증한다 — fake 의 이점과 실스토어의 이점을 동시에 얻는 유일한
지점이다. 기존 fixture(`tests/test_add_edge_endpoint_guard.py`)와 같은 구성이다.
"""
from __future__ import annotations

import ast
import inspect
import json
import pathlib
from collections import Counter

import pytest

from opencrab.ontology.builder import OntologyBuilder
from opencrab.pack import load as pack_load
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore

WRITE_FUNCS = {
    "delete_pack", "load_nodes", "load_nodes_incremental",
    "load_edges", "load_chunks", "load_chunks_incremental", "incremental_finalize",
}


# ───────────────────────── 계약: 가드 커버리지 ─────────────────────────

class TestGuardCoverageContract:
    """쓰기 함수 전량이 ``require_live_data()`` 를 부른다.

    이관 전에는 호출자 리포의 정적 게이트가 이 검사를 했다. 함수가 이 패키지로 넘어왔으니
    **정의를 소유한 쪽**이 계약도 소유한다. 호출자 리포의 게이트는 라이브 적재 직전의
    2차 방어로 남는다(같은 검사를 양쪽에서 하는 것은 중복이 아니라, 어느 한쪽만 배포된
    상황에서도 성립해야 하기 때문이다).

    진입점에서 한 번만 부르는 방식으로는 부족하다 — 진입점을 안 거치고 이 함수들을 직접
    호출하는 경로가 실제로 3종 있었고, 그 경로들이 통째로 무가드였다.
    """

    def test_every_write_function_calls_the_guard(self):
        src = pathlib.Path(pack_load.__file__).read_text(encoding="utf-8")
        defined = {
            n.name: n for n in ast.parse(src).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing = []
        for name in sorted(WRITE_FUNCS):
            fn = defined.get(name)
            if fn is None:
                missing.append(f"{name}: 정의 자체가 없다 — 이름이 바뀌었으면 이 목록도 고쳐라")
                continue
            called = {
                x.func.id for x in ast.walk(fn)
                if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
            }
            if "require_live_data" not in called:
                missing.append(f"{name}: require_live_data() 를 부르지 않는다 (line {fn.lineno})")
        assert not missing, "무가드 쓰기 함수:\n  " + "\n  ".join(missing)

    def test_guard_is_called_before_any_store_write(self):
        """가드가 함수 **첫 문장**이어야 한다.

        중간에 있으면 그 앞의 스토어 접근이 무가드로 실행된다. 순서를 안 보면
        가드를 함수 끝으로 옮기는 변이가 위 테스트를 그대로 통과한다.
        """
        src = pathlib.Path(pack_load.__file__).read_text(encoding="utf-8")
        defined = {
            n.name: n for n in ast.parse(src).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        late = []
        for name in sorted(WRITE_FUNCS):
            body = defined[name].body
            first = body[1] if isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant) else body[0]
            call = getattr(first, "value", None)
            ok = (isinstance(first, ast.Expr) and isinstance(call, ast.Call)
                  and isinstance(call.func, ast.Name)
                  and call.func.id == "require_live_data")
            if not ok:
                late.append(f"{name}: 첫 문장이 가드가 아니다 ({ast.dump(first)[:60]}…)")
        assert not late, "가드가 늦게 불린다:\n  " + "\n  ".join(late)

    def test_guard_ctx_names_the_function(self):
        """가드에 넘기는 ctx 가 함수 이름과 같아야 진단이 쓸모 있다."""
        src = pathlib.Path(pack_load.__file__).read_text(encoding="utf-8")
        defined = {
            n.name: n for n in ast.parse(src).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        wrong = []
        for name in sorted(WRITE_FUNCS):
            for x in ast.walk(defined[name]):
                if (isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                        and x.func.id == "require_live_data"):
                    got = x.args[0].value if x.args and isinstance(x.args[0], ast.Constant) else None
                    if got != name:
                        wrong.append(f"{name}: ctx={got!r}")
        assert not wrong, "가드 ctx 가 함수 이름과 다르다:\n  " + "\n  ".join(wrong)


class TestModuleIsALibrary:
    def test_no_basic_config_at_import(self):
        """``logging.basicConfig`` 는 라이브러리가 부르면 안 된다 — 호출자의 로깅 설정을
        가로챈다. 이관 전 파일에는 있었고, 그것은 스크립트라서 정당했다. 여기서는 아니다."""
        src = pathlib.Path(pack_load.__file__).read_text(encoding="utf-8")
        assert "basicConfig" not in src

    def test_logger_is_module_scoped(self):
        assert pack_load.log.name == "opencrab.pack.load"


class TestBatched:
    @pytest.mark.parametrize("n,size,want", [
        (0, 500, []), (1, 500, [1]), (500, 500, [500]),
        (501, 500, [500, 1]), (5, 2, [2, 2, 1]),
    ])
    def test_partition_sizes(self, n, size, want):
        assert [len(b) for b in pack_load._batched(list(range(n)), size)] == want

    def test_partition_is_lossless_and_ordered(self):
        seq = list(range(1000))
        flat = [x for b in pack_load._batched(seq, 37) for x in b]
        assert flat == seq, "배치 분할이 원소를 잃거나 순서를 바꿨다"

    def test_default_size_is_under_the_sqlite_parameter_limit(self):
        """기본값이 SQLite 파라미터 상한(999) 밑이어야 한다 — 이 함수의 존재 이유다."""
        assert inspect.signature(pack_load._batched).parameters["size"].default < 999


# ───────────────────────── 행동: 진짜 3스토어 ─────────────────────────

@pytest.fixture
def live(tmp_path, monkeypatch):
    """진짜 SQLite 3스토어 + LOCAL_DATA_DIR 실재. 외부 의존 0."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    graph = LocalGraphStore(str(tmp_path / "graph.db"))
    docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
    sql = SQLStore(f"sqlite:///{tmp_path / 'opencrab.db'}")
    assert graph.available
    yield OntologyBuilder(graph, docs, sql), graph, docs
    graph.close()
    docs.close()


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def _node(**kw):
    row = {"id": "n1", "label": "노드 하나", "node_type": "Document", "space": "resource"}
    row.update(kw)
    return row


class TestCustomPropertiesSurvive:
    """**91만 필드 사고의 회귀 테스트.**

    생산자가 커스텀 필드를 노드 최상위에 펼쳤고 소비자는 중첩 `properties` 만 읽었다.
    파일에는 있고 라이브에는 없는 상태가 게이트 전부를 통과한 채 방치됐다. 두 리포로
    갈라져 있어 왕복 검사가 불가능했던 것이 근본 원인이다 — 이제 가능하다.
    """

    def test_top_level_custom_field_reaches_the_store(self, live, tmp_path):
        builder, graph, _ = live
        f = _write_jsonl(tmp_path / "nodes.jsonl",
                         [_node(id="n1", 발행연도="2026", properties={"중첩": "값"})])
        id_map: dict = {}
        ok, skip, err = pack_load.load_nodes("pack-1", f, builder, id_map)
        assert (ok, skip, err) == (1, 0, 0)

        node = graph.get_node("Document", "n1")
        assert node is not None, "노드가 그래프에 없다"
        props = node.get("properties", node)
        assert props.get("발행연도") == "2026", (
            "최상위 커스텀 필드가 라이브에 도달하지 못했다 — 91만 필드 사고와 같은 형태다")
        assert props.get("중첩") == "값", "중첩 properties 가 유실됐다"

    def test_every_row_kind_carries_pack_id_into_the_store(self, live, tmp_path):
        """**축 전체를 닫는다** — 노드 하나만 걸던 판은 엣지·청크가 통째로 무방비였다.

        앞선 판은 `test_top_level_custom_field_reaches_the_store` 로 **노드만** 걸었다.
        측정해 보니 엣지 `properties` 를 빈 dict 로 만드는 변이도, 청크
        `transform_chunk_meta` 결과를 빈 dict 로 만드는 변이도 **35 passed 를 그대로
        유지했다**(2026-08-10). 사고가 난 축은 "커스텀 필드가 라이브에 도달하는가"인데
        세 종류 중 하나에만 못을 박아 둔 것이다.

        그리고 이 축은 표시용이 아니다. `pack_id` 는 세 종류 **전부**의 커스텀 필드
        안에 있고, 그것이 증분·삭제가 팩을 식별하는 **유일한 키**다:

            노드  properties.pack_id  -> live_pack_state / delete_pack
            엣지  properties.pack_id  -> live_pack_state
            청크  metadata.pack_id    -> live_pack_state / delete_pack

        하나라도 유실되면 그 팩은 증분에 안 보이고 삭제도 안 된다 — 파일에는 있는데
        라이브에서 사라지는 91만 필드 사고와 같은 기전이다. 그래서 스토어를 직접 들여다보지
        않고 **`live_pack_state` 왕복**으로 건다. 그것이 실제 소비 경로다.
        """
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1"), _node(id="n2")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)

        ef = _write_jsonl(tmp_path / "edges.jsonl",
                          [{"id": "e1", "source_id": "n1", "target_id": "n2",
                            "label": "CITES", "properties": {"근거": "본문 3쪽"}}])
        ok, skip, err = pack_load.load_edges("pack-1", ef, builder, id_map)
        assert ok == 1, f"엣지 적재 실패 ok={ok} skip={skip} err={err}"

        cf = _write_jsonl(tmp_path / "chunks.jsonl",
                          [{"id": "c1", "document_id": "n1", "text": "본문",
                            "metadata": {"쪽": "3"}}])
        pack_load.load_chunks("pack-1", cf, _RecordingVec(), docs)

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert set(state["nodes"]) == {"n1", "n2"}, "노드가 pack_id 로 안 찾아진다"
        assert state["edges"], (
            "엣지가 pack_id 로 안 찾아진다 — properties 가 라이브에 도달하지 못했다")
        assert set(state["chunks"]) == {"c1"}, (
            "청크가 pack_id 로 안 찾아진다 — metadata 가 라이브에 도달하지 못했다")

    def test_edge_keeps_the_original_label_after_normalisation(self, live, tmp_path):
        """정규화가 라벨을 바꿔도 **원본은 `source_label` 로 남아야** 한다.

        `resolve_edge` 가 `CITES` -> `cites` 처럼 라벨을 갈아치운다. 원본이 사라지면
        "왜 이 관계가 이렇게 됐는가"를 라이브에서 역추적할 수 없다.
        """
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1"), _node(id="n2")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)
        ef = _write_jsonl(tmp_path / "edges.jsonl",
                          [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "CITES"}])
        pack_load.load_edges("pack-1", ef, builder, id_map)
        row = graph._conn.execute(
            "SELECT properties FROM graph_edges WHERE from_id = ?", ("n1",)).fetchone()
        assert row is not None, "엣지가 그래프에 없다"
        props = json.loads(row[0])
        assert props.get("source_label") == "CITES", (
            f"원본 라벨이 유실됐다: {props}")

    def test_pack_id_in_the_file_never_overwrites_the_loading_pack(self, live, tmp_path):
        """덤프에 내장된 `pack_id` 가 적재 대상 팩을 덮으면 안 된다.

        실사고: 엣지 216,711건이 파일 안의 옛 `pack_id` 로 오염됐다. 원본은
        `origin_pack_id` 로 보존하고 `pack_id` 는 적재 대상으로 강제한다.
        이 분기는 파일이 `properties.pack_id` 를 **갖고 있을 때만** 갈린다 —
        그 입력을 안 주면 강제 로직을 지워도 아무도 모른다.
        """
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1"), _node(id="n2")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)
        ef = _write_jsonl(tmp_path / "edges.jsonl",
                          [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "CITES",
                            "properties": {"pack_id": "낡은-팩"}}])
        pack_load.load_edges("pack-1", ef, builder, id_map)
        props = json.loads(graph._conn.execute(
            "SELECT properties FROM graph_edges WHERE from_id = ?", ("n1",)).fetchone()[0])
        assert props["pack_id"] == "pack-1", "파일의 pack_id 가 적재 대상을 덮었다"
        assert props["origin_pack_id"] == "낡은-팩", "원본 pack_id 가 보존되지 않았다"
        assert pack_load.live_pack_state("pack-1", graph, docs, _NoVec())["edges"], (
            "오염된 pack_id 때문에 엣지가 자기 팩에서 안 보인다")

    def test_id_map_records_space_and_type_for_every_row(self, live, tmp_path):
        """엣지 endpoint 해석이 이 맵에 의존한다. 적재 성공 여부와 무관하게 채워져야 한다."""
        builder, _, _ = live
        f = _write_jsonl(tmp_path / "nodes.jsonl",
                         [_node(id="n1"), _node(id="n2", node_type="Concept", space="concept")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", f, builder, id_map)
        assert set(id_map) == {"n1", "n2"}
        assert id_map["n1"] == ("resource", "Document")


class _NoVec:
    """벡터 축만 비활성화한 스텁. 그래프·문서는 진짜 SQLite 다.

    `live_pack_state` 는 `vec.available` 이 False 면 벡터 조회를 건너뛴다. 노드 증분
    판정에 벡터는 관여하지 않으므로 이 축만 끄고 나머지는 실스토어로 돈다.
    """

    available = False

    def __init__(self):
        self.deleted: list[str] = []

    def delete(self, ids):
        self.deleted.extend(ids)


class TestLoadNodesIncremental:
    def test_identical_row_is_skipped_without_touching_any_store(self, live, tmp_path):
        """**왕복**으로 확인한다: 적재 → 라이브 상태 읽기 → 증분이 same 으로 판정.

        기대값을 손으로 지어내면 `live_pack_state` 가 실제로 무엇을 담는지와 어긋나도
        테스트는 통과한다(처음에 그렇게 썼다가 chg 로 나왔다). 라이브를 실제로 읽는다.
        """
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1", 발행연도="2026")])
        pack_load.load_nodes("pack-1", f, builder, {})

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        live_nodes = state["nodes"]
        assert "n1" in live_nodes, "적재한 노드가 라이브 상태에 안 보인다"

        n_new, n_chg, n_same, skip, err, ids = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, live_nodes, graph, docs)
        assert (n_new, n_chg, n_same, skip, err) == (0, 0, 1, 0, 0), (
            "라이브와 동일한 행이 same 으로 판정되지 않았다 — 매 증분마다 전량 재적재된다")
        assert ids == {"n1"}

    def test_changed_row_is_counted_as_changed_not_new(self, live, tmp_path):
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1", 발행연도="2027")])
        live_nodes = {"n1": ("Document", "resource", {"발행연도": "2026", "pack_id": "pack-1"})}
        n_new, n_chg, n_same, skip, err, _ = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, live_nodes, graph, docs)
        assert (n_new, n_chg, n_same) == (0, 1, 0)

    def test_node_type_change_removes_the_old_row(self, live, tmp_path):
        """타입이 바뀌면 **구 행을 지운 뒤** 새 타입으로 넣어야 한다.

        그래프는 `(node_type, node_id)` 로 행을 잡으므로, 지우지 않으면 같은 id 가
        두 타입으로 동시에 존재하는 고아가 남는다. 엣지 endpoint 해석이 어느 쪽을
        잡을지 비결정이 된다.

        앞선 판은 same/chg/new **카운터만** 걸었고, 그래서 `graph.delete_node` 호출을
        통째로 지우는 변이가 35 passed 를 유지했다(2026-08-10). 카운터가 아니라
        **스토어 상태**를 봐야 갈리는 분기다 — chg 카운트는 어느 쪽이든 1이다.
        """
        builder, graph, docs = live
        f_old = _write_jsonl(tmp_path / "old.jsonl", [_node(id="n1", node_type="Document")])
        pack_load.load_nodes("pack-1", f_old, builder, {})
        assert graph.get_node("Document", "n1") is not None

        live_nodes = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())["nodes"]
        f_new = _write_jsonl(tmp_path / "new.jsonl",
                             [_node(id="n1", node_type="Concept", space="concept")])
        _n, n_chg, _s, _sk, _e, _ids = pack_load.load_nodes_incremental(
            "pack-1", f_new, builder, {}, live_nodes, graph, docs)

        assert n_chg == 1, "타입 변경이 chg 로 세어지지 않았다"
        assert graph.get_node("Concept", "n1") is not None, "새 타입 행이 없다"
        assert graph.get_node("Document", "n1") is None, (
            "구 타입 행이 남았다 — 같은 id 가 두 타입으로 존재하는 고아다")

    def test_unknown_row_is_counted_as_new(self, live, tmp_path):
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1")])
        n_new, n_chg, n_same, _, _, _ = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, {}, graph, docs)
        assert (n_new, n_chg, n_same) == (1, 0, 0)


class TestLoadEdges:
    def _map(self):
        return {"n1": ("resource", "Document"), "n2": ("resource", "Document")}

    def test_edge_with_both_endpoints_is_applied(self, live, tmp_path):
        builder, graph, _ = live
        nf = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1"), _node(id="n2")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)
        ef = _write_jsonl(tmp_path / "edges.jsonl",
                          [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "CITES"}])
        ok, skip, err = pack_load.load_edges("pack-1", ef, builder, id_map)
        assert ok == 1 and err == 0, f"ok={ok} skip={skip} err={err}"

    def test_missing_endpoint_is_skipped_with_a_reason(self, live, tmp_path):
        """사유 집계가 **함수 안**에 있어야 한다 — 호출부에 맡기면 형제 호출부가 빠진다.

        라이브 실손실 131,072 건이 사유 불명으로 누적된 뒤에 그렇게 바뀌었다.
        """
        builder, _, _ = live
        ef = _write_jsonl(tmp_path / "edges.jsonl",
                          [{"id": "e1", "source_id": "n1", "target_id": "없는놈", "label": "CITES"}])
        reasons = Counter()
        ok, skip, err = pack_load.load_edges("pack-1", ef, builder, self._map(), reasons=reasons)
        assert (ok, skip, err) == (0, 1, 0)
        assert reasons, "skip 사유가 집계되지 않았다 — 미적재 엣지의 원인이 증발한다"
        key = next(iter(reasons))
        assert key[0] == "endpoint 미존재" and "tgt" in key[2] + key[3]

    def test_label_lookup_is_case_insensitive(self, live, tmp_path):
        """라벨 매핑은 대소문자를 무시한다.

        이관 과정에서 `lookup_label = raw_label.upper()` 가 **아무도 안 쓰는 죽은 변수**로
        남아 있는 것이 드러났다(호출자 리포는 lint 게이트가 없어 보이지 않았다). 확인해 보니
        `resolve_edge` 가 내부에서 upper() 를 하므로 행동 격차는 없었다. 그 사실을 여기
        고정한다 — 죽은 변수를 지우는 정리 커밋이 행동을 바꾸지 않았음을 이 테스트가 보장한다.
        """
        builder, graph, _ = live
        nf = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1"), _node(id="n2")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)
        lower = _write_jsonl(tmp_path / "e_lower.jsonl",
                             [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "cites"}])
        upper = _write_jsonl(tmp_path / "e_upper.jsonl",
                             [{"id": "e2", "source_id": "n1", "target_id": "n2", "label": "CITES"}])
        # **SUT 대 SUT 비교를 버린다.** 앞선 판은 `load_edges(lower) == load_edges(upper)`
        # 만 봤고, 그래서 양쪽이 똑같이 틀리면 통과했다 — 반전 엣지의 endpoint 교환을
        # 지우는 변이가 35 passed 를 유지했다(적대 검증 실증, 2026-08-10: M17).
        # 자기참조 기대값으로 FAIL 받은 것이 이번이 세 번째다. 독립 기대값으로 바꾼다:
        # 둘 다 relation 은 'cites' 로 정규화되고, source_label 은 **원형 그대로** 남는다.
        # 같은 (from, relation, to) 는 upsert 로 덮이므로 **순차로** 확인한다.
        seen = []
        for f, raw in ((lower, "cites"), (upper, "CITES")):
            ok, skip, err = pack_load.load_edges("p", f, builder, id_map)
            assert (ok, skip, err) == (1, 0, 0), f"{raw}: ok={ok} skip={skip} err={err}"
            rel, props = graph._conn.execute(
                "SELECT relation, properties FROM graph_edges WHERE from_id = ?",
                ("n1",)).fetchone()
            seen.append((rel, json.loads(props)["source_label"]))
        assert [r for r, _ in seen] == ["cites", "cites"], (
            f"대소문자에 따라 relation 이 갈렸다: {seen}")
        assert [lbl for _, lbl in seen] == ["cites", "CITES"], (
            f"원본 라벨이 보존되지 않았다 — 라이브에서 역추적이 불가능해진다: {seen}")


class TestDeletePack:
    """`--fresh` 재적재의 삭제 경로. 여기가 새면 재적재가 중복을 쌓는다."""

    def test_deletes_only_the_named_pack(self, live, tmp_path):
        builder, graph, docs = live
        _write_jsonl(tmp_path / "a.jsonl", [_node(id="a1"), _node(id="a2")])
        _write_jsonl(tmp_path / "b.jsonl", [_node(id="b1")])
        pack_load.load_nodes("pack-a", tmp_path / "a.jsonl", builder, {})
        pack_load.load_nodes("pack-b", tmp_path / "b.jsonl", builder, {})

        node_del, _chunk_sql_del, chunk_vec_del = pack_load.delete_pack("pack-a", graph, docs, _NoVec())

        # 관측 가능한 상태가 판정의 본체다. 카운터는 그 다음이다.
        assert graph.get_node("Document", "a1") is None
        assert graph.get_node("Document", "a2") is None
        assert graph.get_node("Document", "b1") is not None, (
            "다른 팩의 노드까지 지웠다 — 팩 경계가 무너졌다")
        assert chunk_vec_del == 0, "vec.available 이 False 면 벡터 삭제는 0이어야 한다"
        # `node_del` 은 **노드 수가 아니다.** 그래프 행 + doc_nodes 보강 행의 합이라
        # 노드 2개에 4가 나온다(출력 라벨도 "노드+엣지"다). 호출부가 이것을 노드 수로
        # 읽으면 3원 대사가 어긋난다 — 그 의미를 여기 못박는다.
        assert node_del == 4, (
            f"node_del 은 그래프 행 + doc_nodes 보강 행의 합이다 (실제 {node_del})")

    def test_deleted_pack_disappears_from_live_state(self, live, tmp_path):
        """왕복: 적재 → 삭제 → 라이브 상태가 비어 있다."""
        builder, graph, docs = live
        _write_jsonl(tmp_path / "a.jsonl", [_node(id="a1")])
        pack_load.load_nodes("pack-a", tmp_path / "a.jsonl", builder, {})
        assert pack_load.live_pack_state("pack-a", graph, docs, _NoVec())["nodes"]

        pack_load.delete_pack("pack-a", graph, docs, _NoVec())
        assert pack_load.live_pack_state("pack-a", graph, docs, _NoVec())["nodes"] == {}

    def test_deleting_an_absent_pack_is_a_noop_not_an_error(self, live):
        builder, graph, docs = live
        assert pack_load.delete_pack("없는-팩", graph, docs, _NoVec()) == (0, 0, 0)


class _RecordingVec:
    """벡터 축만 기록 스텁. 문서 스토어는 진짜 SQLite 그대로다.

    벡터 백엔드는 배치 실패 주입이 필요한데(아래 배치 폴백 검사) 실스토어로는 결정적으로
    실패시킬 수 없다. 실패 주입이 필요한 축에만 스텁을 쓰는 것이 이 리포의 관례다
    (`tests/test_impact_graph_paths.py` 의 FakeSQLStore 와 같은 계층).
    """

    def __init__(self, fail_batches_larger_than: int | None = None):
        self.available = True
        self.calls: list[tuple[int, list[str]]] = []
        self.ids: list[str] = []
        self.metas: dict[str, dict] = {}      # 벡터에 실제로 도달한 메타
        self.deleted: list[str] = []
        self._fail_over = fail_batches_larger_than

    def upsert_texts(self, texts, metadatas=None, ids=None):
        """**실 스토어와 같은 시그니처**여야 한다 — `(texts, metadatas, ids)`.

        앞선 판은 `(texts, ids, metadatas)` 로 순서를 뒤집어 선언했다. `load.py` 는
        전부 키워드로 부르므로 실동작은 무사했지만, 스텁이 실계약을 반대로 선언한
        탓에 **위치인자 축의 모든 테스트가 구조적으로 무력**했다(적대 검증 실증,
        2026-08-10: N15 — 건별 재시도를 위치인자로 바꾸면 id 와 메타가 뒤바뀌는데
        50 passed). 스텁이 실계약을 잘못 베끼면 스텁 자체가 사각지대다.

        길이 검증도 실 스토어를 따른다 — 실제로는 `ValueError` 인데 스텁이 `zip` 으로
        조용히 절단하면 길이 불일치 변이가 안 보인다(N13).
        """
        ids = list(ids or [])
        metadatas = list(metadatas or [{}] * len(texts))
        if not (len(texts) == len(metadatas) == len(ids)):
            raise ValueError("texts, metadatas, and ids must have the same length.")
        self.calls.append((len(ids), list(ids)))
        if self._fail_over is not None and len(ids) > self._fail_over:
            raise RuntimeError("주입된 배치 실패")
        self.ids.extend(ids)
        # **metadatas 를 반드시 보관한다.** 앞선 판은 이 인자를 버렸고, 그래서 청크 메타를
        # 통째로 폐기하는 변이가 35 passed 를 유지했다(적대 검증 실증, 2026-08-10: M18b).
        # 그 변이는 문서화된 실사고(청크 메타 30필드 소실)의 재현이다. 스텁이 실스토어보다
        # 관대하면 스텁이 곧 사각지대가 된다.
        for i, meta in zip(ids, metadatas):
            self.metas[i] = dict(meta or {})

    def delete(self, ids):
        """`incremental_finalize` 가 부른다. 이게 없으면 그 함수를 아예 호출할 수 없다."""
        self.deleted.extend(ids)


def _chunk(i, text="본문"):
    return {"id": f"c{i}", "document_id": "n1", "text": text}


class TestLoadChunks:
    def test_chunks_reach_both_vector_and_doc_stores(self, live, tmp_path):
        _builder, _graph, docs = live
        f = _write_jsonl(tmp_path / "chunks.jsonl", [_chunk(1), _chunk(2)])
        vec = _RecordingVec()
        ok, err = pack_load.load_chunks("pack-1", f, vec, docs)
        assert (ok, err) == (2, 0)
        assert vec.ids == ["c1", "c2"]
        row = docs._conn.execute(
            "SELECT text FROM doc_sources WHERE source_id = ?", ("c1",)).fetchone()
        assert row is not None, "벡터에는 들어갔는데 문서 스토어에는 없다"

    def test_unavailable_vector_store_skips_without_error(self, live, tmp_path):
        _builder, _graph, docs = live
        f = _write_jsonl(tmp_path / "chunks.jsonl", [_chunk(1)])
        assert pack_load.load_chunks("pack-1", f, _NoVec(), docs) == (0, 0)

    def test_duplicate_ids_keep_only_the_first(self, live, tmp_path):
        _builder, _graph, docs = live
        f = _write_jsonl(tmp_path / "chunks.jsonl",
                         [_chunk(1, "처음"), _chunk(1, "나중"), _chunk(2)])
        vec = _RecordingVec()
        ok, err = pack_load.load_chunks("pack-1", f, vec, docs)
        assert (ok, err) == (2, 0), "중복 ID 가 dedup 되지 않았다"
        assert vec.ids == ["c1", "c2"]

    def test_batch_boundary_flushes(self, live, tmp_path):
        _builder, _graph, docs = live
        f = _write_jsonl(tmp_path / "chunks.jsonl", [_chunk(i) for i in range(5)])
        vec = _RecordingVec()
        pack_load.load_chunks("pack-1", f, vec, docs, batch_size=2)
        assert [n for n, _ in vec.calls] == [2, 2, 1], (
            f"배치 경계에서 flush 되지 않았다: {[n for n, _ in vec.calls]}")

    def test_failed_batch_retries_one_by_one(self, live, tmp_path):
        """배치 1건의 결함이 배치 전체를 날리면 안 된다 — 건별 재시도 폴백."""
        _builder, _graph, docs = live
        f = _write_jsonl(tmp_path / "chunks.jsonl", [_chunk(i) for i in range(3)])
        vec = _RecordingVec(fail_batches_larger_than=1)
        ok, err = pack_load.load_chunks("pack-1", f, vec, docs)
        assert (ok, err) == (3, 0), "배치 실패 후 건별 재시도가 전부 살리지 못했다"
        assert [n for n, _ in vec.calls] == [3, 1, 1, 1], (
            f"배치 1회 실패 후 건별 3회여야 한다: {[n for n, _ in vec.calls]}")


class TestChunksIncremental:
    """`load_chunks_incremental` — 재임베딩 생략 판정.

    이 함수는 커버리지 0이었다(486-574 전량 미커버). 그래서 "라이브 대조를 아예 안 해서
    매 증분마다 전량 재임베딩"과 "메타만 바뀐 행을 same 으로 흘려 라이브가 영구 스테일"
    두 변이가 35 passed 를 유지했다(적대 검증 실증, 2026-08-10: M15·M12).

    카운터만 보면 안 갈린다 — **임베딩 호출 여부**와 **doc_sources 실제 갱신**을 본다.
    """

    def test_identical_chunk_is_skipped_without_re_embedding(self, live, tmp_path):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1, "본문")])
        vec = _RecordingVec()
        pack_load.load_chunks("pack-1", f, vec, docs)
        live_chunks = {sid: (txt, json.loads(md)) for sid, txt, md in docs._conn.execute(
            "SELECT source_id, text, metadata FROM doc_sources")}

        vec2 = _RecordingVec()
        c_new, c_txt, c_meta, c_same, err, ids = pack_load.load_chunks_incremental(
            "pack-1", f, vec2, docs, live_chunks)
        assert (c_new, c_txt, c_meta, c_same, err) == (0, 0, 0, 1, 0), (
            f"동일 청크가 same 이 아니다: new={c_new} txt={c_txt} meta={c_meta} same={c_same}")
        assert vec2.calls == [], (
            "동일 청크인데 임베딩을 다시 호출했다 — 매 증분마다 전량 재임베딩된다")
        assert ids == {"c1"}

    def test_text_change_triggers_re_embedding(self, live, tmp_path):
        _b, _g, docs = live
        f1 = _write_jsonl(tmp_path / "c1.jsonl", [_chunk(1, "처음")])
        pack_load.load_chunks("pack-1", f1, _RecordingVec(), docs)
        live_chunks = {sid: (txt, json.loads(md)) for sid, txt, md in docs._conn.execute(
            "SELECT source_id, text, metadata FROM doc_sources")}

        f2 = _write_jsonl(tmp_path / "c2.jsonl", [_chunk(1, "바뀐 본문")])
        vec = _RecordingVec()
        _n, c_txt, _m, c_same, _e, _i = pack_load.load_chunks_incremental(
            "pack-1", f2, vec, docs, live_chunks)
        assert (c_txt, c_same) == (1, 0), f"텍스트 변경이 txt 로 안 세어졌다 ({c_txt},{c_same})"
        assert vec.ids == ["c1"], "텍스트가 바뀌었는데 재임베딩하지 않았다"

    def test_metadata_only_change_updates_the_store_without_re_embedding(self, live, tmp_path):
        """메타만 바뀌면 재임베딩은 생략하되 **문서 스토어는 실제로 갱신**해야 한다.

        카운터만 보는 테스트는 "meta 로 세고 아무것도 안 하는" 변이를 통과시킨다.
        라이브 메타가 영구히 스테일해진다.
        """
        _b, _g, docs = live
        f1 = _write_jsonl(tmp_path / "c1.jsonl",
                          [{"id": "c1", "document_id": "n1", "text": "본문",
                            "metadata": {"쪽": "3"}}])
        pack_load.load_chunks("pack-1", f1, _RecordingVec(), docs)
        live_chunks = {sid: (txt, json.loads(md)) for sid, txt, md in docs._conn.execute(
            "SELECT source_id, text, metadata FROM doc_sources")}

        f2 = _write_jsonl(tmp_path / "c2.jsonl",
                          [{"id": "c1", "document_id": "n1", "text": "본문",
                            "metadata": {"쪽": "99"}}])
        vec = _RecordingVec()
        _n, _t, c_meta, c_same, _e, _i = pack_load.load_chunks_incremental(
            "pack-1", f2, vec, docs, live_chunks)
        assert (c_meta, c_same) == (1, 0), f"메타 변경이 meta 로 안 세어졌다 ({c_meta},{c_same})"
        assert vec.calls == [], "메타만 바뀌었는데 재임베딩했다"
        stored = json.loads(docs._conn.execute(
            "SELECT metadata FROM doc_sources WHERE source_id = ?", ("c1",)).fetchone()[0])
        assert stored.get("쪽") == "99", (
            f"meta 로 세었지만 문서 스토어가 갱신되지 않았다 — 라이브가 영구 스테일이다: {stored}")


class TestIncrementalFinalizeSafetyPins:
    """`incremental_finalize` 의 안전핀 — **삭제 권한을 가진 함수**인데 커버리지 0이었다.

    595-744 전량 미커버였고, 그래서 30% 삭제폭주 핀을 무력화해도, 0-항목 핀을 통과시켜
    라이브 노드를 **전멸**시켜도 35 passed 가 유지됐다(적대 검증 실증, 2026-08-10:
    M8·M16). 실측 피해:

        M16 0-항목 핀 통과   -> node_del=10 · 라이브 노드 10 -> 0 (전멸)
        M8  30% 핀 무력화    -> node_del=9  · 라이브 노드 10 -> 1

    안전핀은 "평소엔 아무 일도 안 하는" 코드라 테스트가 없으면 **있는지조차 알 수 없다.**
    """

    def _seed(self, builder, docs, tmp_path, n=10, pack="pack-1"):
        rows = [_node(id=f"n{i}") for i in range(n)]
        f = _write_jsonl(tmp_path / "nodes.jsonl", rows)
        pack_load.load_nodes(pack, f, builder, {})
        return {r["id"] for r in rows}

    def _live(self, graph, docs, pack="pack-1"):
        return pack_load.live_pack_state(pack, graph, docs, _NoVec())

    def test_zero_bypack_nodes_aborts_instead_of_deleting_everything(self, live, tmp_path):
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path)
        state = self._live(graph, docs)
        assert len(state["nodes"]) == 10
        with pytest.raises(SystemExit) as ei:
            pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                set(), set(), set(), False, 0, 0)
        assert "by-pack 파일 누락 의심" in str(ei.value)
        assert len(self._live(graph, docs)["nodes"]) == 10, "중단했는데 뭔가 지워졌다"

    def test_zero_bypack_chunks_aborts_too(self, live, tmp_path):
        """0-항목 핀은 **노드와 청크 둘**이다. 노드만 걸면 청크 핀이 무방비다.

        노드 핀만 테스트하던 판은 청크 핀을 통째로 통과시키는 변이가 46 passed 를
        유지했다(자체 측정, 2026-08-10: M16c). 축을 열어 놓고 한 점만 고정하는 실수를
        **이 파일 안에서 또** 저지른 것이라 즉시 닫는다.
        """
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path)
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1)])
        pack_load.load_chunks("pack-1", f, _RecordingVec(), docs)
        state = self._live(graph, docs)
        assert state["chunks"], "전제: 라이브에 청크가 있어야 한다"
        with pytest.raises(SystemExit) as ei:
            pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                {f"n{i}" for i in range(10)}, set(), set(), False, 10, 0)
        assert "by-pack 청크 0건" in str(ei.value), str(ei.value)
        assert self._live(graph, docs)["chunks"], "중단했는데 청크가 지워졌다"

    def test_deletion_ratio_over_thirty_percent_aborts(self, live, tmp_path):
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path)
        state = self._live(graph, docs)
        keep = {"n0"}                       # 9/10 = 90% 삭제 후보
        with pytest.raises(SystemExit) as ei:
            pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                keep, set(), set(), False, len(keep), 0)
        assert "삭제 후보 비율 초과" in str(ei.value)
        assert "--force-delete" in str(ei.value), "강행 방법을 알려야 한다"
        assert len(self._live(graph, docs)["nodes"]) == 10, "중단했는데 뭔가 지워졌다"

    def test_force_delete_bypasses_the_ratio_pin(self, live, tmp_path):
        """핀은 **우회 가능해야** 한다 — 안 그러면 정당한 대량 정리가 막힌다.

        이게 없으면 `force_delete` 분기를 통째로 지우는 변이가 안 잡힌다.
        """
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path)
        state = self._live(graph, docs)
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n0"}, set(), set(), True, 1, 0)
        assert res["node_del"] == 9, f"강행했는데 9건이 안 지워졌다: {res}"
        assert set(self._live(graph, docs)["nodes"]) == {"n0"}

    def test_ratio_under_the_pin_deletes_normally(self, live, tmp_path):
        """정상 경로 — 핀이 항상 중단시키면 증분 정리가 통째로 죽는다."""
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path)
        state = self._live(graph, docs)
        keep = {f"n{i}" for i in range(8)}   # 2/10 = 20%
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            keep, set(), set(), False, len(keep), 0)
        assert res["node_del"] == 2, res
        assert set(self._live(graph, docs)["nodes"]) == keep


class TestReversedEdgeSwapsEndpoints:
    """`HAS_PART` 같은 반전 관계는 **endpoint 도 함께 바뀌어야** 한다.

    `resolve_edge("HAS_PART", …)` 는 `part_of` 로 정규화하면서 `reversed=True` 를 준다.
    `n1 HAS_PART n2` 는 의미상 `n2 part_of n1` 이다. endpoint 를 안 바꾸면 그래프에
    **방향이 뒤집힌 관계**가 들어가고, 영향도 분석이 통째로 거꾸로 나온다.

    앞선 판은 `load_edges(lower) == load_edges(upper)` 라는 SUT 대 SUT 비교뿐이라
    교환을 지우는 변이가 35 passed 를 유지했다(적대 검증 실증, 2026-08-10: M17).
    반전 관계 라벨을 아예 입력에 안 넣었으므로 그 분기가 실행조차 안 됐다.
    """

    def test_endpoints_are_swapped_for_a_reversed_relation(self, live, tmp_path):
        builder, graph, _ = live
        # `part_of` 는 concept->concept 에서만 허용된다(grammar). 공간을 안 맞추면
        # 반전 분기에 닿기도 전에 문법 위반으로 skip 돼 테스트가 무의미해진다.
        nf = _write_jsonl(tmp_path / "nodes.jsonl",
                          [_node(id="n1", node_type="Concept", space="concept"),
                           _node(id="n2", node_type="Concept", space="concept")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)

        from opencrab.pack.normalize import resolve_edge
        assert resolve_edge("HAS_PART", "concept", "concept")[3] is True, (
            "전제: HAS_PART 가 반전 관계여야 이 테스트가 의미를 갖는다")

        ef = _write_jsonl(tmp_path / "edges.jsonl",
                          [{"id": "e1", "source_id": "n1", "target_id": "n2",
                            "label": "HAS_PART"}])
        applied: set = set()
        ok, skip, err = pack_load.load_edges("pack-1", ef, builder, id_map, applied=applied)
        assert (ok, skip, err) == (1, 0, 0), f"ok={ok} skip={skip} err={err}"

        row = graph._conn.execute(
            "SELECT from_id, relation, to_id FROM graph_edges").fetchone()
        assert tuple(row) == ("n2", "part_of", "n1"), (
            f"반전 관계인데 endpoint 가 안 바뀌었다: {tuple(row)} — 방향이 거꾸로 들어갔다")
        assert applied == {("n2", "part_of", "n1")}, (
            f"applied 도 반전 **후** 기준이어야 증분 정리가 같은 것을 본다: {applied}")


class _SqliteVecLike:
    """sqlite-vec 백엔드 흉내 — `_conn`/`_table`/`pack_id` 컬럼을 갖는다.

    `_NoVec`(available=False)만 쓰던 판은 `delete_pack` 의 벡터 분기(`load.py:119-138`)가
    **한 번도 실행되지 않았고**, 그래서 그 분기를 통째로 죽이는 변이가 46 passed 를
    유지했다(적대 검증 실증, 2026-08-10: M20). `--fresh` 재적재 때 벡터 고아가 남는다.
    스텁이 실스토어의 형태를 안 흉내내면 그 경로는 영원히 미검증이다.
    """

    _table = "vectors_kure"

    def __init__(self):
        import sqlite3
        self.available = True
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            f"CREATE TABLE {self._table} (node_id TEXT PRIMARY KEY, pack_id TEXT)")
        self._conn.commit()

    def seed(self, pack, ids):
        self._conn.executemany(
            f"INSERT INTO {self._table} VALUES (?, ?)", [(i, pack) for i in ids])
        self._conn.commit()

    def rows(self):
        return {(r[0], r[1]) for r in
                self._conn.execute(f"SELECT node_id, pack_id FROM {self._table}")}


class TestDeletePackVectorBranch:
    def test_vectors_of_the_named_pack_are_deleted(self, live, tmp_path):
        _b, graph, docs = live
        vec = _SqliteVecLike()
        vec.seed("pack-a", ["a1", "a2"])
        vec.seed("pack-b", ["b1"])

        _n, _c, chunk_vec_del = pack_load.delete_pack("pack-a", graph, docs, vec)

        assert chunk_vec_del == 2, f"벡터 2건이 지워져야 한다 (실제 {chunk_vec_del})"
        assert vec.rows() == {("b1", "pack-b")}, (
            f"다른 팩의 벡터까지 지웠거나 대상이 남았다: {vec.rows()}")

    def test_vector_branch_is_skipped_when_unavailable(self, live):
        _b, graph, docs = live
        assert pack_load.delete_pack("없는-팩", graph, docs, _NoVec()) == (0, 0, 0)


class TestIncrementalFinalizeActuallyDeletes:
    """**핀을 통과한 뒤 실제로 무엇을 지우는가.**

    앞선 판은 안전핀 5건을 걸었지만 `incremental_finalize` 의 **삭제·정리 본체**는
    여전히 0줄 커버였다. 그래서 "지웠다고 보고하고 안 지우기", "다른 팩 엣지 삭제",
    "앵커 삭제", "살아있는 청크·노드의 벡터 삭제" 가 전부 50 passed 를 유지했다
    (적대 검증 실증, 2026-08-10: N1b·N2·N3·N4·N5·N6·N10·N12).

    핀은 "언제 멈추는가"만 본다. 멈추지 않았을 때 무엇이 사라지는지는 별개 계약이다.
    """

    def _seed_nodes(self, builder, tmp_path, ids, pack="pack-1"):
        f = _write_jsonl(tmp_path / f"n_{pack}.jsonl", [_node(id=i) for i in ids])
        pack_load.load_nodes(pack, f, builder, {})

    def _live(self, graph, docs, vec=None, pack="pack-1"):
        return pack_load.live_pack_state(pack, graph, docs, vec or _NoVec())

    def test_chunk_deletion_is_committed_and_visible_to_another_connection(
            self, live, tmp_path):
        """삭제가 **별도 커넥션에서도** 보여야 한다 — `commit` 누락이 갈리는 지점이다.

        같은 커넥션으로만 확인하면 미커밋 삭제가 통과한다(N10). 그리고 카운트만 맞추고
        실제로 안 지우는 변이(N1b)도 여기서 갈린다.
        """
        import sqlite3
        builder, graph, docs = live
        self._seed_nodes(builder, tmp_path, ["n1"])
        # 청크 4개 중 1개만 삭제 후보 = 25% — 30% 핀에 안 걸리게 한다.
        # (핀은 별도 테스트가 건다. 여기서 보는 것은 **핀을 통과한 뒤**의 삭제다.)
        cf = _write_jsonl(tmp_path / "c.jsonl", [_chunk(i) for i in range(1, 5)])
        pack_load.load_chunks("pack-1", cf, _RecordingVec(), docs)
        state = self._live(graph, docs)
        assert set(state["chunks"]) == {"c1", "c2", "c3", "c4"}

        keep = {"c1", "c2", "c3"}
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n1"}, keep, set(), False, 1, len(keep))

        assert res["chunk_del"] == 1, res
        other = sqlite3.connect(f"file:{tmp_path / 'doc.db'}?mode=ro", uri=True)
        try:
            left = {r[0] for r in other.execute("SELECT source_id FROM doc_sources")}
        finally:
            other.close()
        assert left == keep, (
            f"별도 커넥션에서 본 청크가 {left} (기대 {keep}) — 삭제가 커밋되지 않았거나 "
            "지웠다고 보고만 하고 실제로는 안 지웠다")

    def test_stale_edge_cleanup_only_touches_its_own_pack(self, live, tmp_path):
        """stale 엣지 정리가 **자기 팩 엣지만** 지워야 한다."""
        builder, graph, docs = live
        # **순서가 계약이다.** `graph_edges` PK 는 `(from_type,from_id,relation,to_type,to_id)`
        # 라 pack_id 를 안 담는다. 그래서 삭제 SQL 의 pack_id 필터는 "상태를 포착한 뒤
        # 다른 팩이 같은 triple 을 가져간" 경우에만 의미를 갖는다 — 진입점이 상태를 먼저
        # 포착하고 여러 팩을 순차 적재하므로 실제로 도달 가능한 순서다.
        # 그 순서를 안 만들면 필터를 지워도 아무 일이 없다(자체 측정: 필터 제거 시 59 passed).
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1"), _node(id="n2")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)
        pack_load.load_nodes("다른팩", nf, builder, id_map)
        ef = _write_jsonl(tmp_path / "e.jsonl",
                          [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "CITES"}])
        pack_load.load_edges("pack-1", ef, builder, id_map)

        state = self._live(graph, docs)                 # ① pack-1 상태 포착
        assert state["edges"], "전제: 포착 시점에 pack-1 이 그 엣지를 갖는다"
        pack_load.load_edges("다른팩", ef, builder, id_map)   # ② 다른 팩이 같은 triple 을 가져간다
        owner = json.loads(graph._conn.execute(
            "SELECT properties FROM graph_edges").fetchone()[0]).get("pack_id")
        assert owner == "다른팩", f"전제: 소유가 넘어가야 한다 (현재 {owner})"

        # ③ pack-1 이 낡은 상태로 정리 — 자기 것이 아닌 행을 지우면 안 된다
        pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n1", "n2"}, set(), {("n1", "cites", "n9")}, True, 2, 0)

        left = {json.loads(r[0]).get("pack_id") for r in graph._conn.execute(
            "SELECT properties FROM graph_edges")}
        assert "다른팩" in left, (
            f"소유가 넘어간 엣지를 지웠다 — 남은 pack_id={left}. 삭제 SQL 의 pack_id "
            "필터가 없으면 낡은 상태로 남의 팩 행을 지운다")

    def test_empty_applied_edges_skips_cleanup(self, live, tmp_path):
        """`applied_edges` 가 비면 엣지 정리를 **건너뛰어야** 한다.

        edges.jsonl 누락 의심 상황이다 — 그대로 진행하면 라이브 엣지가 전량 삭제된다.
        """
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1"), _node(id="n2")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)
        ef = _write_jsonl(tmp_path / "e.jsonl",
                          [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "CITES"}])
        pack_load.load_edges("pack-1", ef, builder, id_map)
        before = graph._conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
        assert before == 1

        state = self._live(graph, docs)
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n1", "n2"}, set(), set(), True, 2, 0)      # applied 가 비었다

        after = graph._conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
        assert res["edge_del"] == 0 and after == 1, (
            f"반영 엣지 0건인데 정리를 진행했다 — edge_del={res['edge_del']}, 남은 {after}건")

    def test_anchor_nodes_are_never_deletion_candidates(self, live, tmp_path):
        """`dataset:` 앵커는 by-pack 에 없어도 삭제 후보에서 빠져야 한다."""
        builder, graph, docs = live
        self._seed_nodes(builder, tmp_path, ["n1", "dataset:앵커"])
        state = self._live(graph, docs)
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n1"}, set(), set(), True, 1, 0)            # 앵커는 by-pack 에 없다
        left = set(self._live(graph, docs)["nodes"])
        assert "dataset:앵커" in left, f"앵커를 지웠다 — 남은 노드 {left}, node_del={res['node_del']}"

    def test_vector_orphan_cleanup_excludes_this_run(self, live, tmp_path):
        """이번 적재의 노드·청크 id 는 벡터 고아 삭제에서 **둘 다** 빠져야 한다.

        하나만 빼면 살아있는 쪽의 벡터가 지워진다(N4·N6).
        """
        builder, graph, docs = live
        self._seed_nodes(builder, tmp_path, ["n1"])
        cf = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1)])
        pack_load.load_chunks("pack-1", cf, _RecordingVec(), docs)

        class _VecWithIds(_NoVec):
            available = True
            _table = "vectors_kure"

            def __init__(self, ids):
                super().__init__()
                import sqlite3
                self._conn = sqlite3.connect(":memory:")
                self._conn.execute(
                    f"CREATE TABLE {self._table} (node_id TEXT, pack_id TEXT)")
                self._conn.executemany(
                    f"INSERT INTO {self._table} VALUES (?, 'pack-1')", [(i,) for i in ids])
                self._conn.commit()

        vec = _VecWithIds(["n1", "c1", "고아1"])
        state = pack_load.live_pack_state("pack-1", graph, docs, vec)
        pack_load.incremental_finalize(
            "pack-1", graph, docs, vec, state,
            {"n1"}, {"c1"}, set(), True, 1, 1)
        assert "n1" not in vec.deleted, f"살아있는 노드의 벡터를 지웠다: {vec.deleted}"
        assert "c1" not in vec.deleted, f"살아있는 청크의 벡터를 지웠다: {vec.deleted}"


class TestStubsMatchTheRealStoreContract:
    """스텁이 실 스토어 시그니처와 **일치**해야 한다.

    앞선 판의 `_RecordingVec.upsert_texts` 는 `(texts, ids, metadatas)` 였는데
    실 스토어는 `(texts, metadatas=None, ids=None)` 다. `load.py` 가 전부 키워드로
    부르므로 실동작은 무사했지만, **스텁이 실계약을 반대로 선언한 탓에 위치인자 축의
    테스트가 전부 무력**했다(적대 검증 실증, 2026-08-10: N15·N13).

    "스텁을 실계약에 맞추자"는 산문으로는 다음 스텁에서 또 어긋난다. 시그니처를
    코드로 대사한다 — 스텁이 실계약을 잘못 베끼는 것이 **구조적으로 불가능**해진다.
    """

    @pytest.mark.parametrize("method", ["upsert_texts", "delete"])
    def test_recording_vec_signature_matches_the_real_store(self, method):
        from opencrab.stores.sqlite_vec_store import SqliteVecStore
        real = inspect.signature(getattr(SqliteVecStore, method))
        stub = inspect.signature(getattr(_RecordingVec, method))
        assert list(stub.parameters) == list(real.parameters), (
            f"_RecordingVec.{method} 의 파라미터가 실 스토어와 다르다:\n"
            f"  실  {list(real.parameters)}\n  스텁 {list(stub.parameters)}")

    def test_length_mismatch_raises_like_the_real_store(self):
        """실 스토어는 길이 불일치를 `ValueError` 로 거부한다 — 스텁도 그래야 한다.

        `zip` 으로 조용히 절단하면 메타가 한 건 모자란 변이가 안 보인다.
        """
        vec = _RecordingVec()
        with pytest.raises(ValueError, match="same length"):
            vec.upsert_texts(texts=["a", "b"], metadatas=[{}], ids=["x", "y"])

    def test_every_vec_stub_covers_the_methods_load_actually_calls(self):
        """`load.py` 가 부르는 벡터 메서드를 스텁이 전부 갖고 있는가.

        빠진 메서드가 있으면 그 경로는 테스트에서 **실행조차 안 된다** — `_NoVec` 만
        쓰던 판에서 `delete_pack` 의 벡터 분기가 그랬다(M20).
        """
        src = pathlib.Path(pack_load.__file__).read_text(encoding="utf-8")
        called = {
            x.func.attr for x in ast.walk(ast.parse(src))
            if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
            and isinstance(x.func.value, ast.Name) and x.func.value.id == "vec"
        }
        # `available` 은 속성이라 Call 에 안 잡힌다. 메서드만 본다.
        missing = sorted(called - set(dir(_RecordingVec)))
        assert not missing, (
            f"load.py 가 vec.{missing} 를 부르는데 _RecordingVec 에 없다 — "
            "그 경로는 테스트에서 실행조차 안 된다")


class TestGuardActuallyFires:
    """AST 계약은 "부른다"만 본다. 실제로 **막는지**는 행동으로 확인해야 한다."""

    @pytest.mark.parametrize("name", sorted(WRITE_FUNCS))
    def test_write_function_aborts_without_live_data(self, name, monkeypatch, tmp_path):
        monkeypatch.delenv("LOCAL_DATA_DIR", raising=False)
        fn = getattr(pack_load, name)
        n = len(inspect.signature(fn).parameters)
        with pytest.raises(SystemExit) as ei:
            fn(*([None] * n))
        assert "LOCAL_DATA_DIR" in str(ei.value)
        assert f"[{name}]" in str(ei.value), "어느 함수가 걸렸는지 말해야 한다"


class TestPackLiveCountsIsTheSingleSourceOfTruth:
    """`pack_live_counts` 가 라이브 4축 대사 SQL 의 **유일한 자리**인가.

    이 쿼리는 한동안 두 벌이었다 — `incremental_finalize` 안에 한 벌, 호출자 리포의
    대사 스크립트에 또 한 벌. 스토어 스키마가 바뀌면 한쪽만 고쳐지고, 그 어긋남은
    "카운트가 안 맞는다"로만 보여 원인 추적이 어렵다. 스키마 세부는 스토어를 가진 쪽이
    정본이어야 한다는 것이 이 이관의 전제다(2026-08-11 통합).

    **엣지축이 왜 있는가**: 노드·문서·벡터만 대사하던 동안 by-pack 대비 미적재 엣지
    187,069건(15팩)이 전 팩 "정상" 판정을 통과했다. 축을 하나 빼면 그 축의 결손은
    영영 안 보인다 — 그래서 축 집합 자체를 계약으로 건다.
    """

    AXES = {"nodes", "edges", "docs", "vectors"}

    def _stores(self, tmp_path):
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
        return (LocalGraphStore(str(tmp_path / "graph.db")),
                LocalSQLDocStore(str(tmp_path / "doc.db")),
                None)

    def test_returns_exactly_four_axes(self, tmp_path):
        graph, docs, vec = self._stores(tmp_path)
        got = pack_load.pack_live_counts("아무팩", graph, docs, vec)
        assert set(got) == self.AXES, (
            f"축 집합이 달라졌다: {sorted(got)} — 축을 빼면 그 축의 결손이 영영 안 보인다")
        assert all(isinstance(v, int) for v in got.values())

    def test_empty_store_is_all_zero_not_missing(self, tmp_path):
        """없는 팩은 **0** 이다. 키가 빠지면 호출자가 KeyError 로 죽는다."""
        graph, docs, vec = self._stores(tmp_path)
        assert pack_load.pack_live_counts("없는팩", graph, docs, vec) \
            == dict.fromkeys(self.AXES, 0)

    def test_vector_store_without_conn_yields_zero_not_crash(self, tmp_path):
        """벡터 백엔드가 `_conn` 을 안 내주는 경우가 있다 — 죽지 말고 0 이어야 한다."""
        graph, docs, _ = self._stores(tmp_path)

        class NoConn:
            pass

        assert pack_load.pack_live_counts("p", graph, docs, NoConn())["vectors"] == 0

    def test_the_sql_lives_here_only(self):
        """호출자가 같은 쿼리를 다시 선언하지 않는가 — **구조로** 건다.

        문서에 "정본은 하나"라고 적는 것으로는 두 벌이 되는 것을 못 막는다. 실제로
        그렇게 두 벌이 됐다. 이 모듈 안에 카운트 SQL 이 있는지만 확인하고, 호출자 쪽은
        그 리포의 게이트가 본다(단방향 의존이라 여기서 호출자를 알 수 없다).
        """
        import inspect

        src = inspect.getsource(pack_load.pack_live_counts)
        for table in ("graph_nodes", "graph_edges", "doc_sources"):
            assert f"FROM {table}" in src, f"{table} 카운트가 정본에서 사라졌다"
        assert src.count("SELECT COUNT(*)") == 4, (
            "카운트 쿼리 수가 4가 아니다 — 축을 늘렸으면 AXES 와 이 수도 같이 고쳐라")
