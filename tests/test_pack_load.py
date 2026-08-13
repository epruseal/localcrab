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
from opencrab.pack.normalize import transform_chunk_meta
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
            "pack-1", f, builder, {}, live_nodes, graph, docs, {})
        assert (n_new, n_chg, n_same, skip, err) == (0, 0, 1, 0, 0), (
            "라이브와 동일한 행이 same 으로 판정되지 않았다 — 매 증분마다 전량 재적재된다")
        assert ids == {"n1"}

    def test_changed_row_is_counted_as_changed_not_new(self, live, tmp_path):
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1", 발행연도="2027")])
        live_nodes = {"n1": ("Document", "resource", {"발행연도": "2026", "pack_id": "pack-1"})}
        n_new, n_chg, n_same, skip, err, _ = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, live_nodes, graph, docs, {})
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
            "pack-1", f_new, builder, {}, live_nodes, graph, docs, {})

        assert n_chg == 1, "타입 변경이 chg 로 세어지지 않았다"
        assert graph.get_node("Concept", "n1") is not None, "새 타입 행이 없다"
        assert graph.get_node("Document", "n1") is None, (
            "구 타입 행이 남았다 — 같은 id 가 두 타입으로 존재하는 고아다")

    def test_unknown_row_is_counted_as_new(self, live, tmp_path):
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1")])
        n_new, n_chg, n_same, _, _, _ = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, {}, graph, docs, {})
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
        # `node_del` 은 **노드 수가 아니다.** 그래프 행 + `doc_nodes` **보강** 행의 합이다.
        # 호출부가 이것을 노드 수로 읽으면 3원 대사가 어긋난다 — 그 의미를 못박는다.
        #
        # 한동안 이 값이 4 였다(노드 2개인데). 첫 루프의 `docs.delete_node_doc()` 이
        # 조용히 실패하고 `except` 로 빠져서, 뒤의 "graph 트윈 없이 남은 앵커 정리"가
        # 같은 2행을 다시 지웠기 때문이다. 상류가 그 삭제를 고치면서(#100 계열)
        # 보강분이 0 이 됐다 — **동작 개선이지 퇴행이 아니다.**
        #
        # 그래서 숫자 하나를 박지 않고 **구성**을 건다: 보강분은 첫 루프가 놓친 것만
        # 세야 하고, 정상 경로에서는 0 이어야 한다. 그것이 이 값의 계약이다.
        assert node_del == 2, (
            f"node_del 이 {node_del} 이다 — 노드 2개면 그래프 행 2 + 보강 0 이어야 한다. "
            "4 가 나오면 첫 루프의 doc_nodes 삭제가 다시 실패해 같은 행을 두 번 세는 것이다")

        # **보강 경로가 죽지는 않았는가.** 위 단언만으로는 그 코드를 통째로 지워도 통과한다.
        # graph 트윈 없이 `doc_nodes` 에만 있는 앵커를 심어 실제로 정리되는지 본다.
        # 스키마를 읽어서 NOT NULL 컬럼을 채운다 — 손으로 적으면 스키마가 바뀔 때
        # 이 테스트만 조용히 깨진다(실제로 `updated_at` 을 빠뜨려 IntegrityError 가 났다).
        cols = {r[1]: r for r in docs._conn.execute("PRAGMA table_info(doc_nodes)")}
        vals = {"space": "resource", "node_id": "orphan-1",
                "properties": json.dumps({"pack_id": "pack-c"})}
        for name, info in cols.items():
            if name in vals or info[4] is not None:      # 이미 채웠거나 기본값 있음
                continue
            if info[3]:                                   # NOT NULL
                vals[name] = "1970-01-01T00:00:00Z" if "at" in name else ""
        docs._conn.execute(
            f"INSERT INTO doc_nodes ({','.join(vals)}) "
            f"VALUES ({','.join('?' * len(vals))})", tuple(vals.values()))
        docs._conn.commit()
        n2, *_ = pack_load.delete_pack("pack-c", graph, docs, _NoVec())
        assert n2 == 1, (
            f"graph 트윈 없는 doc_nodes 앵커가 안 지워졌다 (실제 {n2}) — "
            "보강 경로가 죽었다. backfill 이 만든 앵커가 팩 삭제 후에도 남는다")

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

    def __init__(self, fail_batches_larger_than: int | None = None,
                 supports_meta_update: bool = True):
        self.available = True
        self.calls: list[tuple[int, list[str]]] = []
        self.ids: list[str] = []
        self.metas: dict[str, dict] = {}      # 벡터에 실제로 도달한 메타
        self.deleted: list[str] = []
        self._fail_over = fail_batches_larger_than
        # **메타만 갱신하는 경로를 흉내낸다.** 실 `SqliteVecStore` 는 `_conn`/`_table` 을
        # 노출해 `UPDATE ... SET metadata` 가 되는데, 스텁이 그걸 안 흉내내면
        # 적재기가 "이 백엔드는 메타 갱신을 못 한다"고 판단해 재임베딩으로 우회한다 —
        # 스텁 한계가 실동작처럼 보인다. 이 파일 docstring 이 경고하는 그 함정이다.
        self.meta_updates: list[tuple[str, dict]] = []
        self._supports_meta_update = supports_meta_update

    def update_metadata(self, chunk_id: str, meta: dict) -> bool:
        """메타만 갱신. `supports_meta_update=False` 면 미지원 백엔드를 흉내낸다."""
        if not self._supports_meta_update:
            return False
        self.meta_updates.append((chunk_id, meta))
        self.metas[chunk_id] = meta
        return True

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


def _expect_node_chunk_ratio_msg(pack, node_cand, node_live, chunk_cand, chunk_live):
    """`incremental_finalize` 의 노드/청크 30% 핀 메시지를 입력 산술에서 그대로
    재구성한다(load.py 의 f-string 과 1:1 대응, 앵커된 `==` 단언용).

    분모 표시는 `len(live_*)` 그대로(0 이어도 그대로 0) — 나눗셈에만 `max(1, …)`
    을 쓰는 load.py 의 계약과 정확히 같다.
    """
    node_ratio = node_cand / max(1, node_live)
    chunk_ratio = chunk_cand / max(1, chunk_live)
    return (
        f"ERROR: [{pack}] 삭제 후보 비율 초과 — "
        f"노드 {node_cand}/{node_live}({node_ratio:.1%}) "
        f"청크 {chunk_cand}/{chunk_live}({chunk_ratio:.1%}) — "
        "--force-delete 로 강행하십시오."
    )


def _expect_doc_ratio_msg(pack, doc_cand, doc_denominator):
    """doc 축 30% 핀 메시지 재구성(load.py:1019-1022 과 1:1 대응)."""
    doc_ratio = doc_cand / doc_denominator
    return (
        f"ERROR: [{pack}] doc 삭제 후보 비율 초과 — "
        f"{doc_cand}/{doc_denominator}({doc_ratio:.1%}) — "
        "--force-delete 로 강행하십시오."
    )


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
        assert str(ei.value) == _expect_node_chunk_ratio_msg("pack-1", 9, 10, 0, 0), str(ei.value)
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

    def test_exactly_thirty_percent_is_allowed_not_aborted(self, live, tmp_path):
        """**경계 자체**를 건다 — 위 셋은 값만 걸고 비교 방향을 안 건다.

        `node_ratio > 0.30` 을 `>=` 로 바꾸는 변이는 "초과 시 중단"(90%)·"force 우회"·
        "미만 시 정상"(20%) 셋을 **전부 통과한다** — 어느 것도 정확히 0.30 을 안 지난다.
        실측(2026-08-11): 그 변이로 68개가 그대로 green 이었다.

        그런데 방향이 바뀌면 **정확히 30%인 팩이 삭제에서 빠진다.** 정상 정리가 조용히
        멈추는 것이고, 운영자는 "왜 안 지워지지"만 보게 된다. 값과 방향은 같이 걸어야 한다
        (호출자 리포의 `SCORE_THRESHOLD` 계약이 같은 이유로 비교 방향을 함께 건다).
        """
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path)
        state = self._live(graph, docs)
        keep = {f"n{i}" for i in range(7)}   # 3/10 = 정확히 0.30
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            keep, set(), set(), False, len(keep), 0)
        assert res["node_del"] == 3, f"정확히 30%는 핀에 걸리면 안 된다: {res}"
        assert set(self._live(graph, docs)["nodes"]) == keep

    def test_just_over_thirty_percent_aborts(self, live, tmp_path):
        """경계 바로 위 — 위 테스트와 짝이다.

        둘을 같이 걸어야 `>` 를 `>=` 로도, `0.30` 을 `0.31` 로도 못 바꾼다.
        한쪽만 걸면 반대쪽으로 밀 수 있다.
        """
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path)
        state = self._live(graph, docs)
        keep = {f"n{i}" for i in range(6)}   # 4/10 = 0.40, 경계 바로 위
        with pytest.raises(SystemExit) as ei:
            pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                keep, set(), set(), False, len(keep), 0)
        assert str(ei.value) == _expect_node_chunk_ratio_msg("pack-1", 4, 10, 0, 0), str(ei.value)
        assert len(self._live(graph, docs)["nodes"]) == 10, "중단했는데 뭔가 지워졌다"


class TestIncrementalFinalize:
    """`incremental_finalize` 전용 계약을 **한 클래스**에서 5축 전부 건다(2026-08-11 추가 지시).

    적대 검증 실증: 0-항목 안전핀을 `pass` 로 무력화해도 스위트 전체가 green 이었다 —
    그 정도로 이 함수는(삭제 권한을 가진 유일한 함수인데도) 전용 커버리지가 없었다.

    아래 1·2축의 상세 근거·변이 실측(그리고 30% 핀의 값+방향 경계)은
    `TestIncrementalFinalizeSafetyPins` 에 이미 있고, 앵커 보호(`dataset:` 접두사)는
    `TestIncrementalFinalizeActuallyDeletes` 에 이미 있다 — 여기서는 그 계약이
    **이 클래스 하나만 읽어도** 5축 전부 보이도록 다시 걸고(짧게, 근거는 원 클래스
    참조), 아직 어디에도 없던 두 축(`created_by=title-backfill` 앵커,
    `had_write_failures`)을 새로 채운다. `had_write_failures` 쪽이 특히 중요하다 —
    이번 리뷰로 처음 생긴 안전핀인데 지금까지 테스트가 하나도 없었다.
    """

    def _seed(self, builder, docs, tmp_path, n=10, pack="pack-1"):
        rows = [_node(id=f"n{i}") for i in range(n)]
        f = _write_jsonl(tmp_path / "nodes.jsonl", rows)
        pack_load.load_nodes(pack, f, builder, {})
        return {r["id"] for r in rows}

    def _live(self, graph, docs, pack="pack-1"):
        return pack_load.live_pack_state(pack, graph, docs, _NoVec())

    # ── 1. 0-item 안전핀 (근거: TestIncrementalFinalizeSafetyPins) ────────

    def test_zero_item_pin_fires_for_nodes_and_chunks(self, live, tmp_path):
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path)
        state = self._live(graph, docs)
        with pytest.raises(SystemExit):
            pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                set(), set(), set(), False, 0, 0)
        assert len(self._live(graph, docs)["nodes"]) == 10, "중단했는데 노드가 지워졌다"

        cf = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1)])
        pack_load.load_chunks("pack-1", cf, _RecordingVec(), docs)
        state2 = self._live(graph, docs)
        with pytest.raises(SystemExit):
            pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state2,
                {f"n{i}" for i in range(10)}, set(), set(), False, 10, 0)
        assert self._live(graph, docs)["chunks"], "중단했는데 청크가 지워졌다"

    # ── 2. 30% 안전핀 — 값과 방향 + force_delete 우회 ──────────────────

    @pytest.mark.parametrize("keep_n, expect_abort", [
        (7, False),   # 3/10 = 정확히 0.30 — 통과해야 한다(`>` 지 `>=` 아님)
        (6, True),    # 4/10 = 0.40 — 발동해야 한다
    ])
    def test_thirty_percent_pin_value_and_direction(
            self, live, tmp_path, keep_n, expect_abort):
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path)
        state = self._live(graph, docs)
        keep = {f"n{i}" for i in range(keep_n)}
        if expect_abort:
            with pytest.raises(SystemExit) as ei:
                pack_load.incremental_finalize(
                    "pack-1", graph, docs, _NoVec(), state,
                    keep, set(), set(), False, len(keep), 0)
            assert str(ei.value) == _expect_node_chunk_ratio_msg(
                "pack-1", 10 - keep_n, 10, 0, 0), str(ei.value)
        else:
            res = pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                keep, set(), set(), False, len(keep), 0)
            assert res["node_del"] == 10 - keep_n, res

    def test_force_delete_bypasses_thirty_percent_pin(self, live, tmp_path):
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path)
        state = self._live(graph, docs)
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n0"}, set(), set(), True, 1, 0)
        assert res["node_del"] == 9, res

    # ── 3. 앵커 보호 — dataset: 접두사(근거: TestIncrementalFinalizeActuallyDeletes)
    #      + created_by=title-backfill(여기서 처음 건다) ────────────────

    def test_title_backfill_anchor_is_protected_like_dataset_prefix(self, live, tmp_path):
        """`_is_anchor` 의 두 조건 중 `dataset:` 접두사만 기존 커버리지가 있었다 —
        `created_by=title-backfill` 조건은 지금까지 무테스트였다."""
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [
            _node(id="n1"),
            _node(id="backfilled-1", properties={"created_by": "title-backfill"}),
        ])
        pack_load.load_nodes("pack-1", nf, builder, {})
        state = self._live(graph, docs)
        assert "backfilled-1" in state["nodes"], "사전 조건: 앵커가 라이브에 있어야 한다"

        # backfilled-1 은 by-pack 에 없다고 신고 — 앵커가 아니면 삭제 후보가 된다.
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n1"}, set(), set(), True, 1, 0)

        left = set(self._live(graph, docs)["nodes"])
        assert "backfilled-1" in left, (
            f"title-backfill 앵커를 지웠다 — 남은 노드 {left}, node_del={res['node_del']}")

    # ── 4. 삭제 카운터가 요청 수가 아니라 실제 성공 수를 반영하는가 ──────

    def test_edge_del_reflects_actual_deletion_not_requested(self, live, tmp_path):
        """`stale_edges` 후보에 있어도 `graph_edges` 에 실제로 없으면 `edge_del` 을 세면 안 된다.

        무조건 `edge_del += 1` 이던 판은 이런 "요청은 했지만 대상이 없던" 경우도 세서,
        아무것도 안 지워졌는데 지웠다고 보고했다. `cur.rowcount` 로 세는 지금 계약을 건다.
        """
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1"), _node(id="n2")])
        pack_load.load_nodes("pack-1", nf, builder, {})
        state = self._live(graph, docs)
        # graph_edges 에 실제로 없는 유령 엣지를 live 엣지 집합에 섞는다.
        state = dict(state)
        state["edges"] = {("n1", "cites", "유령-n9")}

        # applied_edges 를 완전히 비우면 "반영 엣지 0건" 안전핀이 먼저 정리를 통째로
        # 스킵해 버리므로, 무관한 다른 triple 을 넣어 그 핀을 피하면서 유령 엣지는
        # 여전히 stale 후보(=DELETE 대상)로 남게 한다.
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n1", "n2"}, set(), {("무관", "무관계", "무관-y")}, True, 2, 0)
        assert res["edge_del"] == 0, (
            f"실제로 존재하지 않던 엣지인데 edge_del 을 세었다: {res}")

    def test_vec_orphan_del_does_not_count_a_failed_batch(self, live, tmp_path):
        """벡터 삭제가 예외를 던진 배치는 `vec_orphan_del`에 들어가면 안 된다.

        `vec.delete()`에는 실제 삭제를 확인할 API가 없어 성공 시엔 "요청 수"를 셀
        수밖에 없다(코드 주석 참조) — 그래도 **예외가 난 배치**까지 세면 안 된다.
        """
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path, n=1)
        cf = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1)])
        pack_load.load_chunks("pack-1", cf, _RecordingVec(), docs)

        class _VecThatAlwaysFailsDelete(_NoVec):
            available = True

            def delete(self, ids):
                raise RuntimeError("주입된 벡터 삭제 실패")

        vec = _VecThatAlwaysFailsDelete()
        state = pack_load.live_pack_state("pack-1", graph, docs, vec)
        state = dict(state)
        state["vec_ids"] = {"고아-벡터-1"}   # 스텁은 실제로 벡터 id 를 못 내므로 직접 채운다

        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, vec, state,
            {"n0"}, {"c1"}, set(), True, 1, 1)
        assert res["vec_orphan_del"] == 0, (
            f"벡터 삭제가 예외를 던졌는데 vec_orphan_del 을 세었다: {res}")

    # ── 5. had_write_failures 안전핀은 제거됐다(F5 설계 결정) ─────────────
    #
    # 삭제 축 전부가 저장 성공이 아니라 **파일**에서 보호 집합을 만든다
    # (`bypack_ids.add` 는 저장 **전**에 실행된다) — 저장 실패한 행은 이미
    # 삭제 후보가 아니므로, 이 핀은 보호를 더하지 않고 정상 정리만 막았다.
    # 중립성(핀 제거가 무해함)의 회귀 방지는 `TestPinRemovalIsNeutralAcrossSinks`
    # 가 노드·엣지·청크 세 sink 각각으로 건다.

    def test_positional_call_without_a_had_write_failures_kwarg_still_hits_the_zero_item_pin(
            self, live, tmp_path):
        """이름을 고쳤다 — 예전엔 `had_write_failures=False` 기본값이 종전 동작을
        지키는지를 걸었는데, 그 인자 자체가 지금은 없다(위 참고). 남은 것은
        시그니처가 여전히 위치인자 11개뿐이라는 것과, 순수 위치인자 호출도
        0-item 안전핀(SystemExit)을 정상 발동시킨다는 것 — 이 둘을 지금 실제로
        검증하는 이름·docstring 으로 바꾼다. 예전엔 위치인자 개수가 우연히
        맞아서만 통과했다.
        """
        builder, graph, docs = live
        self._seed(builder, docs, tmp_path)
        state = self._live(graph, docs)
        params = inspect.signature(pack_load.incremental_finalize).parameters
        assert "had_write_failures" not in params, (
            "had_write_failures 가 되살아났다 — 삭제 축 전부가 파일 기준 보호 집합을 "
            "쓰므로 이 핀은 보호를 더하지 않고 정상 정리만 막는다(F5)")
        with pytest.raises(SystemExit):
            pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                set(), set(), set(), False, 0, 0)   # 순수 위치인자, kwarg 없음


class TestRatioPinAxisIsolation:
    """G1 — 노드·청크·doc 3축 30% 핀이 서로 **격리**돼 있는가.

    적대 검증 실증(2026-08-11): 노드/청크 핀(`node_ratio > 0.30 or chunk_ratio >
    0.30`)만 무력화한(0.30→1.30) 사본에서도 `pytest -q` 가 114 passed 였다 — doc 핀
    메시지가 기존 `"삭제 후보 비율 초과" in str(ei.value)` 부분문자열 단언을 그대로
    만족시켜, 노드·청크 축 자체가 무력화된 것을 아무도 못 잡았다.

    이 클래스는 축마다 **다른 두 축을 0(또는 <30%)으로 고정**한 입력을 쓰고,
    기대 메시지를 입력 산술에서 그대로 포맷해(`_expect_*_msg`) `==` 로 전체 대조한다
    (부분문자열 `in` 금지 — v10 검수 조건).
    """

    def _seed_nodes(self, builder, tmp_path, ids, pack="pack-1"):
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id=i) for i in ids])
        pack_load.load_nodes(pack, f, builder, {})

    def _live(self, graph, docs, pack="pack-1"):
        return pack_load.live_pack_state(pack, graph, docs, _NoVec())

    # ── 노드 축 격리 (청크 0 · doc 축 완전 비움) ────────────────────────

    def test_node_only_exactly_thirty_percent_is_not_aborted(self, live, tmp_path):
        builder, graph, docs = live
        ids = [f"n{i}" for i in range(10)]
        self._seed_nodes(builder, tmp_path, ids)
        for nid in ids:                       # doc 축을 완전히 비워 노드 축만 남긴다
            docs.delete_node_doc("resource", nid)
        state = self._live(graph, docs)
        assert state["doc_node_spaces"] == {}, "전제: doc 축이 비어 있어야 노드 축만 걸린다"
        chunk_ratio = 0 / max(1, len(state["chunks"]))
        assert chunk_ratio < 0.30, "전제(자기 단언): 청크 축은 0 이어야 한다"

        keep = {f"n{i}" for i in range(7)}    # 3/10 = 정확히 0.30
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            keep, set(), set(), False, len(keep), 0)
        assert res["node_del"] == 3, f"정확히 30%는 핀에 걸리면 안 된다: {res}"

    def test_node_only_over_thirty_percent_aborts(self, live, tmp_path):
        builder, graph, docs = live
        ids = [f"n{i}" for i in range(10)]
        self._seed_nodes(builder, tmp_path, ids)
        for nid in ids:
            docs.delete_node_doc("resource", nid)
        state = self._live(graph, docs)
        assert state["doc_node_spaces"] == {}, "전제: doc 축이 비어 있어야 노드 축만 걸린다"
        chunk_ratio = 0 / max(1, len(state["chunks"]))
        assert chunk_ratio < 0.30, "전제(자기 단언): 청크 축은 0 이어야 한다"

        keep = {"n0"}                          # 9/10 = 90%
        with pytest.raises(SystemExit) as ei:
            pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                keep, set(), set(), False, len(keep), 0)
        assert str(ei.value) == _expect_node_chunk_ratio_msg("pack-1", 9, 10, 0, 0), str(ei.value)
        assert len(self._live(graph, docs)["nodes"]) == 10, "중단했는데 뭔가 지워졌다"

    # ── 청크 축 격리 (노드·doc 축 전부 비움 — 노드를 아예 안 심는다) ──────

    def test_chunk_only_exactly_thirty_percent_is_not_aborted(self, live, tmp_path):
        _builder, graph, docs = live
        cf = _write_jsonl(tmp_path / "c.jsonl", [_chunk(i) for i in range(10)])
        pack_load.load_chunks("pack-1", cf, _RecordingVec(), docs)
        state = self._live(graph, docs)
        assert state["nodes"] == {} and state["doc_node_spaces"] == {}, (
            "전제(자기 단언): 노드·doc 축이 완전히 비어 있어야 청크 축만 격리된다")

        keep_chunks = {f"c{i}" for i in range(7)}   # 3/10 = 정확히 0.30
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            set(), keep_chunks, set(), False, 0, len(keep_chunks))
        assert res["chunk_del"] == 3, f"정확히 30%는 핀에 걸리면 안 된다: {res}"

    def test_chunk_only_over_thirty_percent_aborts(self, live, tmp_path):
        _builder, graph, docs = live
        cf = _write_jsonl(tmp_path / "c.jsonl", [_chunk(i) for i in range(10)])
        pack_load.load_chunks("pack-1", cf, _RecordingVec(), docs)
        state = self._live(graph, docs)
        assert state["nodes"] == {} and state["doc_node_spaces"] == {}, (
            "전제(자기 단언): 노드·doc 축이 완전히 비어 있어야 청크 축만 격리된다")

        keep_chunks = {f"c{i}" for i in range(6)}   # 4/10 = 40%
        with pytest.raises(SystemExit) as ei:
            pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                set(), keep_chunks, set(), False, 0, len(keep_chunks))
        assert str(ei.value) == _expect_node_chunk_ratio_msg("pack-1", 0, 0, 4, 10), str(ei.value)
        assert len(self._live(graph, docs)["chunks"]) == 10, "중단했는데 청크가 지워졌다"

    # ── doc 축 격리 (노드·청크 축 <30% 로 고정) ─────────────────────────
    #
    # doc 축 분모는 doc_node_spaces 의 **노드 수**다(F4-d) — 행 수가 아니다. 여기서는
    # 그 분모를 노드 축 분모와 **의도적으로 다르게** 만든다(10개 노드 중 3개만
    # doc_node_spaces 에 남긴다). 그러면 같은 3개 후보가 노드 축엔 3/10=30%(경계,
    # 미발동)를, doc 축엔 3/3=100%(발동)을 내 두 축이 실제로 분리됐음이 드러난다.
    # 이 분모(노드 수)가 행 수로 바뀌는 변이는 별도로 `TestDocAxisDenominator` 가
    # 33.3%/20% 조합으로 더 촘촘히 잡는다(G5).

    def test_doc_only_over_thirty_percent_aborts_node_and_chunk_stay_isolated(
            self, live, tmp_path):
        builder, graph, docs = live
        ids = [f"n{i}" for i in range(10)]
        self._seed_nodes(builder, tmp_path, ids)
        for nid in ids[3:]:                    # 7개는 doc 행을 지워 doc 분모를 3으로 좁힌다
            docs.delete_node_doc("resource", nid)
        state = self._live(graph, docs)
        assert set(state["doc_node_spaces"]) == {"n0", "n1", "n2"}, (
            f"전제: doc_node_spaces 가 3개 노드여야 한다: {set(state['doc_node_spaces'])}")

        keep = set(ids) - {"n0"}               # 노드 후보 1개뿐(n0) — doc 후보도 n0 뿐
        node_ratio = 1 / 10
        chunk_ratio = 0 / max(1, len(state["chunks"]))
        assert node_ratio < 0.30 and chunk_ratio < 0.30, (
            "전제(자기 단언): 노드·청크 축은 30% 미만이어야 doc 축만 격리된다")

        with pytest.raises(SystemExit) as ei:
            pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                keep, set(), set(), False, len(keep), 0)
        assert str(ei.value) == _expect_doc_ratio_msg("pack-1", 1, 3), str(ei.value)
        left_spaces = {r[0] for r in docs._conn.execute(
            "SELECT space FROM doc_nodes WHERE node_id=?", ("n0",))}
        assert left_spaces == {"resource"}, "중단했는데 doc 행이 지워졌다"


class TestPinRemovalIsNeutralAcrossSinks:
    """F1 회귀 방지 — `had_write_failures` 안전핀 제거가 **중립**임을 세 sink 각각으로 건다.

    핀이 지키려던 것은 "이번 실행에 저장 실패가 하나라도 있으면 삭제를 통째로
    건너뛴다"였다. 그런데 삭제 후보 집합은 저장 성공 여부가 아니라 **파일**에서
    만들어진다 — 노드는 `bypack_ids.add(node_id)` 가 `add_node` **전**에 실행되고
    (load.py 주석 "add 성공 여부와 무관하게 항상"), 엣지는 `applied.add(...)` 가
    저장 성공/실패와 무관하게 항상 실행되며, 청크는 `bypack_ids.add(chunk_id)` 가
    write 시도 전에 실행된다. 즉 저장이 실패한 행도 이미 보호 집합에 들어 있으므로
    핀이 없어도 그 행은 삭제되지 않는다 — 핀은 보호를 더하지 않고 **무관한** stale
    행의 정상 정리만 막고 있었다.

    아래 세 테스트는 각 sink 에서 (1) 무관한 stale 행이 실제로 지워지고
    (2) 저장 실패한 행 자신은 지워지지 않는다는 것을 **같은 실행**에서 함께 건다.
    """

    def test_node_write_failure_does_not_block_stale_node_cleanup(
            self, live, tmp_path, monkeypatch):
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1"), _node(id="n2")])
        pack_load.load_nodes("pack-1", nf, builder, {})
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert set(state["nodes"]) == {"n1", "n2"}, "전제: 두 노드 다 라이브에 있어야 한다"

        def _broken_upsert_node(*a, **kw):
            raise RuntimeError("주입된 그래프 쓰기 실패")
        monkeypatch.setattr(graph, "upsert_node", _broken_upsert_node)

        # 이번 증분: n1 은 값이 바뀌어 재저장을 시도하지만 실패한다. n2 는 파일에서
        # 아예 빠졌다 — n1 의 실패와 무관한 stale 후보다.
        f2 = _write_jsonl(tmp_path / "n2.jsonl", [_node(id="n1", 발행연도="2027")])
        n_new, n_chg, n_same, skip, err, bypack_ids = pack_load.load_nodes_incremental(
            "pack-1", f2, builder, {}, state["nodes"], graph, docs, {})
        assert err == 1, f"저장 실패가 err 로 안 잡혔다: n_new={n_new} n_chg={n_chg} err={err}"
        assert bypack_ids == {"n1"}, "저장 실패와 무관하게 bypack_ids 는 채워져야 한다"

        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            bypack_ids, set(), set(), True, 1, 0)
        assert res["node_del"] == 1, (
            f"저장 실패가 있었다는 이유로 무관한 stale 노드(n2) 정리가 막혔다: {res}")
        left = set(pack_load.live_pack_state("pack-1", graph, docs, _NoVec())["nodes"])
        assert "n2" not in left, "무관한 stale 노드(n2)가 실제로 안 지워졌다"
        assert "n1" in left, (
            "저장 실패한 행(n1)이 지워졌다 — by-pack 보호 집합에 있어 안 지워져야 한다")

    def test_edge_write_failure_does_not_block_stale_edge_cleanup(
            self, live, tmp_path, monkeypatch):
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1"), _node(id="n2"), _node(id="n3")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)
        ef = _write_jsonl(tmp_path / "e.jsonl", [
            {"id": "e1", "source_id": "n1", "target_id": "n2", "label": "CITES"},
            {"id": "e2", "source_id": "n2", "target_id": "n3", "label": "CITES"},
        ])
        pack_load.load_edges("pack-1", ef, builder, id_map)
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert state["edges"] == {("n1", "cites", "n2"), ("n2", "cites", "n3")}

        def _broken_upsert_edge(*a, **kw):
            raise RuntimeError("주입된 그래프 쓰기 실패")
        monkeypatch.setattr(graph, "upsert_edge", _broken_upsert_edge)

        # 이번 증분: e1 만 재반영을 시도하지만 저장이 실패한다. e2 는 파일에서
        # 아예 빠졌다 — e1 의 실패와 무관한 stale 후보다.
        ef2 = _write_jsonl(tmp_path / "e2.jsonl",
                           [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "CITES"}])
        applied: set = set()
        ok, skip, err = pack_load.load_edges("pack-1", ef2, builder, id_map, applied=applied)
        assert (ok, err) == (0, 1), f"저장 실패가 err 로 안 잡혔다: ok={ok} err={err}"
        assert applied == {("n1", "cites", "n2")}, "저장 실패와 무관하게 applied 는 채워져야 한다"

        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n1", "n2", "n3"}, set(), applied, True, 3, 0)
        assert res["edge_del"] == 1, (
            f"저장 실패가 있었다는 이유로 무관한 stale 엣지(e2) 정리가 막혔다: {res}")
        left = {(r[0], r[1], r[2]) for r in graph._conn.execute(
            "SELECT from_id, relation, to_id FROM graph_edges")}
        assert ("n2", "cites", "n3") not in left, "무관한 stale 엣지(e2)가 실제로 안 지워졌다"
        assert ("n1", "cites", "n2") in left, (
            "저장 실패한 엣지(e1)가 지워졌다 — applied 보호 집합에 있어 안 지워져야 한다")

    def test_chunk_write_failure_does_not_block_stale_chunk_cleanup(self, live, tmp_path):
        builder, graph, docs = live
        cf = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1, "본문1"), _chunk(2, "본문2")])
        pack_load.load_chunks("pack-1", cf, _RecordingVec(), docs)
        live_chunks = {sid: (txt, json.loads(md)) for sid, txt, md in docs._conn.execute(
            "SELECT source_id, text, metadata FROM doc_sources")}
        assert set(live_chunks) == {"c1", "c2"}

        class _VecFailsOn(_NoVec):
            """지정 id 의 `upsert_texts` 호출만 실패시킨다 — 나머지는 실동작(delete)."""
            available = True

            def __init__(self, fail_id):
                super().__init__()
                self._fail_id = fail_id

            def upsert_texts(self, texts, metadatas=None, ids=None):
                if self._fail_id in (ids or []):
                    raise RuntimeError("주입된 벡터 쓰기 실패")

        # 이번 증분: c1 은 텍스트가 바뀌어 재임베딩을 시도하지만 실패한다. c2 는
        # 파일에서 아예 빠졌다 — c1 의 실패와 무관한 stale 후보다.
        cf2 = _write_jsonl(tmp_path / "c2.jsonl", [_chunk(1, "바뀐 본문1")])
        c_new, c_txt, c_meta, c_same, err, bypack_ids = pack_load.load_chunks_incremental(
            "pack-1", cf2, _VecFailsOn("c1"), docs, live_chunks)
        assert err == 1, f"저장 실패가 err 로 안 잡혔다: c_txt={c_txt} err={err}"
        assert bypack_ids == {"c1"}, "저장 실패와 무관하게 bypack_ids 는 채워져야 한다"

        self._seed_one_node(builder, tmp_path)
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"anchor-node"}, bypack_ids, set(), True, 1, 1)
        assert res["chunk_del"] == 1, (
            f"저장 실패가 있었다는 이유로 무관한 stale 청크(c2) 정리가 막혔다: {res}")
        left = {r[0] for r in docs._conn.execute("SELECT source_id FROM doc_sources")}
        assert "c2" not in left, "무관한 stale 청크(c2)가 실제로 안 지워졌다"
        assert "c1" in left, (
            "저장 실패한 청크(c1)가 지워졌다 — by-pack 보호 집합에 있어 안 지워져야 한다")
        row = docs._conn.execute(
            "SELECT text FROM doc_sources WHERE source_id='c1'").fetchone()
        assert row[0] == "본문1", (
            "벡터 쓰기가 실패했는데 doc_sources 가 새 텍스트로 갱신됐다 — "
            f"영구 불일치 (실제 {row[0]!r})")

    def _seed_one_node(self, builder, tmp_path):
        f = _write_jsonl(tmp_path / "anchor.jsonl", [_node(id="anchor-node")])
        pack_load.load_nodes("pack-1", f, builder, {})


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

    def delete(self, ids):
        """`incremental_finalize` 의 벡터 고아 정리가 부르는 일반 삭제 경로.

        `delete_pack` 의 sql 분기는 `_vec_backend()` 를 거쳐 직접 SQL 을 실행하지만
        (위 클래스 docstring), `incremental_finalize` 의 고아 정리는 백엔드 종류와
        무관하게 `vec.delete(ids)` 를 호출한다 — 진짜 sqlite 행을 지워야 "요청 목록"
        이 아니라 **실제 backend row** 로 확인하는 양성 테스트가 성립한다.
        """
        ids = list(ids)
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        self._conn.execute(
            f"DELETE FROM {self._table} WHERE node_id IN ({placeholders})", ids)
        self._conn.commit()


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

    def test_chunk_deletion_batches_correctly_across_the_500_item_boundary(
            self, live, tmp_path):
        """`_batched` 기본 크기(500)를 실제로 넘는 입력으로 배치 경계를 강제한다.

        한 배치만 도는 입력으로는 `chunk_del += cur.rowcount` 를 `= cur.rowcount`
        (마지막 배치만 반영)로 바꾸는 변이나 `placeholders` 개수가 틀어지는 변이가
        안 잡힌다 — 전량이 실제로 지워지는지까지 확인해야 갈린다.
        """
        builder, graph, docs = live
        self._seed_nodes(builder, tmp_path, ["n1"])
        n = 620   # 500 경계를 넘겨 배치 2개(500+120)를 강제한다
        cf = _write_jsonl(tmp_path / "c.jsonl", [_chunk(i) for i in range(n)])
        pack_load.load_chunks("pack-1", cf, _RecordingVec(), docs)
        state = self._live(graph, docs)
        assert len(state["chunks"]) == n

        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n1"}, {"없는-청크"}, set(), True, 1, 0)

        assert res["chunk_del"] == n, (
            f"배치 경계를 넘는 삭제가 전량 반영되지 않았다 (기대 {n}, 실제 {res['chunk_del']})")
        left = docs._conn.execute("SELECT COUNT(*) FROM doc_sources").fetchone()[0]
        assert left == 0, f"배치 경계를 넘는 청크가 실제로는 안 지워졌다 (남은 {left}건)"


class TestIncrementalFinalizePositiveDeletionAcrossAllFourAxes:
    """핀을 통과한 뒤 **4축(노드·청크·엣지·벡터) 전부**가 실제로 지워지는가를 한
    시나리오에서 함께 확인한다(2026-08-11 F5 지시).

    지금까지의 양성 테스트는 축마다 흩어져 있었고, 벡터는 `vec.deleted`(요청
    리스트)만 확인해 `vec.delete()` 가 실제로 아무 것도 안 지워도 통과했다. 여기서는
    삭제 전 대상이 실제로 존재한다는 전제, 그 팩의 candidate 이고 by-pack 에 없는
    것만 지워진다는 것, 0건 핀을 피할 무관한 applied edge, 다른 팩의 동일 relation
    triple 은 살아남는다는 것, 그리고 벡터는 `_SqliteVecLike` 의 진짜 sqlite 테이블을
    **별도 커넥션**으로 읽어 확인하는 것까지 한 실행에서 전부 건다.
    """

    def test_stale_node_chunk_edge_and_vector_orphan_are_all_actually_removed(
            self, live, tmp_path):
        import sqlite3
        builder, graph, docs = live

        nf = _write_jsonl(tmp_path / "n.jsonl",
                          [_node(id="n1"), _node(id="n2"), _node(id="stale-n")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)
        ef = _write_jsonl(tmp_path / "e.jsonl",
                          [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "CITES"}])
        pack_load.load_edges("pack-1", ef, builder, id_map)
        cf = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1), _chunk(2)])
        pack_load.load_chunks("pack-1", cf, _RecordingVec(), docs)

        vec = _SqliteVecLike()
        vec.seed("pack-1", ["c1", "고아-벡터"])

        # 다른 팩이 **같은 relation, 다른 endpoint** 의 엣지를 갖는다 — 살아남아야 한다.
        nf2 = _write_jsonl(tmp_path / "n2.jsonl", [_node(id="m1"), _node(id="m2")])
        pack_load.load_nodes("다른팩", nf2, builder, id_map)
        ef2 = _write_jsonl(tmp_path / "e2.jsonl",
                          [{"id": "e2", "source_id": "m1", "target_id": "m2", "label": "CITES"}])
        pack_load.load_edges("다른팩", ef2, builder, id_map)

        state = pack_load.live_pack_state("pack-1", graph, docs, vec)
        # ── 전제: 삭제 전 대상이 실제로 존재한다 ──
        assert "stale-n" in state["nodes"]
        assert {"c1", "c2"} <= set(state["chunks"])
        assert ("n1", "cites", "n2") in state["edges"]
        assert "고아-벡터" in state["vec_ids"]

        # ── 이번 증분: stale-n·c2 는 by-pack 에서 빠지고, 엣지는 전량 stale
        #    (무관한 applied 항목으로 "반영 엣지 0건" 핀을 피한다) ──
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, vec, state,
            {"n1", "n2"}, {"c1"}, {("무관", "무관계", "무관-y")}, True, 2, 1)

        assert res["node_del"] == 1, res
        assert res["chunk_del"] == 1, res
        assert res["edge_del"] == 1, res
        assert res["vec_orphan_del"] == 1, res

        # ── 별도 커넥션으로 readback — "지웠다고 보고만 하고 실제론 안 지웠다"를 배제한다 ──
        g2 = sqlite3.connect(f"file:{tmp_path / 'graph.db'}?mode=ro", uri=True)
        d2 = sqlite3.connect(f"file:{tmp_path / 'doc.db'}?mode=ro", uri=True)
        try:
            node_ids = {r[0] for r in g2.execute("SELECT node_id FROM graph_nodes")}
            edges = {(r[0], r[1], r[2]) for r in g2.execute(
                "SELECT from_id, relation, to_id FROM graph_edges")}
            chunk_ids = {r[0] for r in d2.execute("SELECT source_id FROM doc_sources")}
        finally:
            g2.close()
            d2.close()

        assert "stale-n" not in node_ids, "stale 노드가 실제로 안 지워졌다"
        assert {"n1", "n2"} <= node_ids, "무관한 살아있는 노드까지 지워졌다"
        assert ("n1", "cites", "n2") not in edges, "stale 엣지가 실제로 안 지워졌다"
        assert ("m1", "cites", "m2") in edges, "다른 팩의 동일 relation triple 이 지워졌다"
        assert "c2" not in chunk_ids, "stale 청크가 실제로 안 지워졌다"
        assert "c1" in chunk_ids, "무관한 살아있는 청크까지 지워졌다"
        assert vec.rows() == {("c1", "pack-1")}, (
            f"벡터 고아가 요청만 되고 실제 backend 행에서는 안 지워졌다: {vec.rows()}")


class TestDeletePackReclaimPredicateIsPackIdOnly:
    """`delete_pack` 의 회수(reclaim) 술어 4표 — `graph_nodes`/`graph_edges`/`doc_nodes`
    는 `pack_id` 단일 키, `doc_sources` 만 `pack_id OR source` 다(load.py 주석
    226-245, 294-300).

    `source`/`source_id`/`pack` 는 소유 키가 아니다 — `transform_node` 가 입력의 외래
    `source` 를 properties 에 그대로 보존하므로, 다른 팩 소유 행에 지금 지우려는
    팩명과 우연히 같은 `source` 값이 실제로 생길 수 있다(예: 예전에 그 팩에서 이
    노드를 참조했던 흔적). 그 값으로 회수되면 남의 팩이 지워진다.
    """

    def test_graph_nodes_reclaim_ignores_a_foreign_source_field(self, live, tmp_path):
        builder, graph, docs = live
        # own-pack 소유 노드인데 properties.source 가 지우려는 팩명과 같다.
        f = _write_jsonl(tmp_path / "n.jsonl",
                         [_node(id="n1", properties={"source": "target"})])
        pack_load.load_nodes("own-pack", f, builder, {})
        assert graph.get_node("Document", "n1") is not None

        pack_load.delete_pack("target", graph, docs, _NoVec())
        assert graph.get_node("Document", "n1") is not None, (
            "source 필드가 지우려는 팩명과 같다는 이유로 다른 팩 소유 노드가 지워졌다")

    def test_graph_edges_reclaim_ignores_a_foreign_source_field(self, live, tmp_path):
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1"), _node(id="n2")])
        id_map: dict = {}
        pack_load.load_nodes("own-pack", nf, builder, id_map)
        ef = _write_jsonl(tmp_path / "e.jsonl",
                          [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "CITES",
                            "properties": {"source": "target"}}])
        pack_load.load_edges("own-pack", ef, builder, id_map)
        before = graph._conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
        assert before == 1

        pack_load.delete_pack("target", graph, docs, _NoVec())
        after = graph._conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
        assert after == 1, "source 필드가 지우려는 팩명과 같다는 이유로 다른 팩 엣지가 지워졌다"

    def test_doc_nodes_reclaim_ignores_a_foreign_source_field(self, live, tmp_path):
        builder, graph, docs = live
        docs.upsert_node_doc("resource", "Document", "orphan-1",
                              {"pack_id": "own-pack", "source": "target"})
        pack_load.delete_pack("target", graph, docs, _NoVec())
        left = docs._conn.execute(
            "SELECT COUNT(*) FROM doc_nodes WHERE node_id=?", ("orphan-1",)).fetchone()[0]
        assert left == 1, "source 필드가 지우려는 팩명과 같다는 이유로 다른 팩 doc_nodes 행이 지워졌다"

    def test_doc_sources_reclaim_matches_pack_id_or_source_but_ignores_other_tags(
            self, live, tmp_path):
        """`doc_sources` 는 **유일하게** `source` 도 본다 — 그래서 `source=target` 은
        실제로 지워져야 하고(양성 절반), `pack_id` 도 `source` 도 아닌 다른 태그가
        같은 값이어도 지워지면 안 된다(음성 절반)."""
        _builder, _graph, docs = live
        docs.upsert_source("c-by-source", "본문", {"source": "target"})
        docs.upsert_source("c-by-unrelated-tag", "본문",
                           {"pack_id": "own-pack", "tag": "target"})

        pack_load.delete_pack("target", _graph, docs, _NoVec())

        left = {r[0] for r in docs._conn.execute("SELECT source_id FROM doc_sources")}
        assert "c-by-source" not in left, (
            "doc_sources 의 source 매치가 실제로 안 지워졌다 — doc_sources 만의 OR 축이 죽었다")
        assert "c-by-unrelated-tag" in left, (
            "pack_id/source 가 아닌 다른 태그가 같은 값이라는 이유로 doc_sources 행이 지워졌다")


class TestDocSourcesReclaimBothDirectionsAndFTSShadowCleanup:
    """G6 — `doc_sources` 회수 양방향(pack_id·source) + FTS5 그림자 테이블 정리.

    FTS 삭제는 **두 독립 경로**에 있다: `delete_pack`(load.py:314) 과
    `incremental_finalize`(load.py:1063). 한쪽만 부르는 테스트는 다른 쪽 DELETE 를
    지워도 통과한다(v10 검수 지적) — 그래서 각 경로를 **별도 테스트**로 건다.

    백엔드 한정: `LocalSQLDocStore` 전용이다(Pg=GIN·Mongo=없음, 2026-08-12 실측).
    `live` fixture 가 이 백엔드를 직접 인스턴스화하므로 별도 분기는 두지 않는다.
    """

    def test_source_only_and_pack_id_only_rows_are_both_reclaimed_unrelated_survives(
            self, live, tmp_path):
        """pack_id 만 태그된 행·source 만 태그된 행 둘 다 회수돼야 한다(양방향) —
        `doc_sources` 회수 술어가 `source` 단독으로 축소되면 pack_id-only 행이,
        `pack_id` 단독으로 축소되면 source-only 행이 각각 안 지워진다."""
        _builder, _graph, docs = live
        docs.upsert_source("c-source-only", "본문A", {"source": "pack-1"})
        docs.upsert_source("c-pack-id-only", "본문B", {"pack_id": "pack-1"})
        docs.upsert_source("c-unrelated", "본문C", {"pack_id": "다른팩", "tag": "pack-1"})

        pack_load.delete_pack("pack-1", _graph, docs, _NoVec())

        left = {r[0] for r in docs._conn.execute("SELECT source_id FROM doc_sources")}
        assert "c-source-only" not in left, "source 만 일치하는 행이 안 지워졌다"
        assert "c-pack-id-only" not in left, "pack_id 만 일치하는 행이 안 지워졌다"
        assert "c-unrelated" in left, "pack_id/source 가 아닌 무관 태그 행이 지워졌다"

    def test_delete_pack_removes_the_fts_shadow_row_and_leaves_unrelated_row(
            self, live, tmp_path):
        """`delete_pack` 경로의 FTS 삭제(load.py:314) — 행 단위 독립 readback."""
        _builder, _graph, docs = live
        if not docs._fts_ok:
            pytest.skip("FTS5 미가용 빌드 — doc_sources_fts 가상 테이블이 없다")

        docs.upsert_source("c1", "삭제 대상 본문", {"pack_id": "pack-1"})
        docs.upsert_source("c2", "무관 본문", {"pack_id": "다른팩"})

        meta_before = json.loads(docs._conn.execute(
            "SELECT metadata FROM doc_sources WHERE source_id=?", ("c1",)).fetchone()[0])
        assert meta_before.get("pack_id") == "pack-1", (
            "전제: c1 이 pack_id 매치로 delete_pack 후보가 되어야 한다 "
            "(delete_pack 은 keep 집합이 없어 pack_id/source 매치 자체가 후보 조건이다)")
        before = {r[0] for r in docs._conn.execute(
            "SELECT source_id FROM doc_sources_fts")}
        assert "c1" in before, "전제: 삭제 전 대상이 FTS 에 있어야 한다"

        node_del, chunk_sql_del, _chunk_vec_del = pack_load.delete_pack(
            "pack-1", _graph, docs, _NoVec())
        assert chunk_sql_del == 1, (node_del, chunk_sql_del)

        after = {r[0] for r in docs._conn.execute(
            "SELECT source_id FROM doc_sources_fts")}
        assert "c1" not in after, f"delete_pack 이 FTS 그림자 행을 안 지웠다: {after}"
        assert "c2" in after, f"무관 행이 delete_pack 의 FTS 삭제에 함께 지워졌다: {after}"

    def test_incremental_finalize_removes_the_fts_shadow_row_and_leaves_unrelated_row(
            self, live, tmp_path):
        """`incremental_finalize` 경로의 FTS 삭제(load.py:1063) — `delete_pack` 과는
        **독립된 코드 경로**라 별도로 걸어야 한다. 행 단위 독립 readback."""
        builder, graph, docs = live
        if not docs._fts_ok:
            pytest.skip("FTS5 미가용 빌드 — doc_sources_fts 가상 테이블이 없다")

        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", nf, builder, {})
        cf = _write_jsonl(tmp_path / "c.jsonl", [_chunk(1), _chunk(2)])
        pack_load.load_chunks("pack-1", cf, _RecordingVec(), docs)
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert set(state["chunks"]) == {"c1", "c2"}, "전제: 삭제 전 대상이 live 에 있어야 한다"

        before = {r[0] for r in docs._conn.execute(
            "SELECT source_id FROM doc_sources_fts")}
        assert "c1" in before, "전제: 삭제 전 대상이 FTS 에 있어야 한다"

        keep_chunks = {"c2"}    # c1 만 후보 — bypack_chunk_ids 에 없다
        assert "c1" not in keep_chunks, "전제: c1 이 bypack_chunk_ids 에 없어야 후보가 된다"
        # 1/2 = 50% > 30% 라 30% 핀에 걸린다 — 이 테스트의 초점은 핀이 아니라 핀을
        # 통과한 뒤의 FTS 삭제이므로 force_delete 로 강행한다(핀 자체는 G1 이 건다).
        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n1"}, keep_chunks, set(), True, 1, len(keep_chunks))
        assert res["chunk_del"] == 1, res

        after = {r[0] for r in docs._conn.execute(
            "SELECT source_id FROM doc_sources_fts")}
        assert "c1" not in after, f"incremental_finalize 가 FTS 그림자 행을 안 지웠다: {after}"
        assert "c2" in after, f"무관 행이 incremental_finalize 의 FTS 삭제에 함께 지워졌다: {after}"


class _FakeChromaCollection:
    """`.get`/`.delete`/`.add`/`.update`/`.upsert` 를 흉내내는 Chroma 컬렉션 더블.

    계약은 **실측**(chromadb 1.5.7, `EphemeralClient` 위에서 프로브 스크립트로 확인,
    2026-08-12)에서 그대로 베꼈다 — 산문으로 짐작하지 않는다:

      · `update`/`upsert` 는 메타데이터를 **병합**한다(겹치는 키만 덮어쓰고 그 외
        키는 존속). 실측: `update(ids=["c1"], metadatas=[{"a": 99}])` 를 `{"a": 1,
        "b": 2}` 위에 부르면 결과가 `{"a": 99, "b": 2}` — "b"가 살아남는다.
      · `update` 를 존재하지 않는 id 에 부르면 **예외 없이 no-op** 이다(실측:
        `col.update(ids=["nope"], ...)` 가 조용히 통과한다).
      · `upsert` 로 `uris` 를 안 주면 기존 uri 가 **보존**된다(실측: uri 를 준
        레코드에 `uris=` 없이 upsert 해도 `get(include=["uris"])` 가 그대로
        돌려준다).
      · `delete` + `add` 는 **치환**이다 — `add` 가 레코드를 처음부터 다시 짓는다
        (실측: delete 뒤 다른 메타로 add 하면 옛 키가 전부 사라진다).
      · `get(ids=..., include=[...])` 는 요청한 축만 채워 돌려준다. `uris` 가
        없는 레코드는 그 자리가 `None` 이다.

    `_NoVec`(available=False)만 쓰던 판은 `delete_pack`/`pack_live_counts`/
    `live_pack_state`/`_vec_meta_update` 의 Chroma 분기가 **한 번도 실행되지
    않았다** — 그 경로는 영원히 미검증이었다(#pgvector 실측과 같은 클래스, F5).

    **결함 주입 축**(R1 게이트 ⑥~⑭ 전용). 프로덕션 계약과 무관한 테스트 전용 표면
    이다 — 기본값(빈 set)에서는 아무것도 바꾸지 않는다.

      · `fail_get_calls`: `get(ids=...)` 호출 **순번**(1부터) 이 이 집합에 있으면
        예외를 던진다 — `_vec_meta_update` 는 get 을 선(존재확인)·후(검증) 두 번
        부르므로 순번으로 어느 쪽을 실패시킬지 고른다.
      · `fail_delete_ids` / `fail_add_ids`: 그 id 에 대한 `delete`/`add` 호출이
        예외를 던진다(둘 다 상태 변형 **전에** 던져 "실패=무변형"을 흉내낸다).
      · `lossy_add_ids`: `add` 가 예외 없이 "성공"하지만 메타만 쓰고 임베딩·문서는
        비워 둔다 — v11 검수가 잡은 "메타만 남는 lossy add" 를 재현한다.
    """

    def __init__(self, rows):
        self._rows: dict[str, str] = dict(rows)       # {node_id: pack_id}
        self.embeddings: dict[str, list[float]] = {}
        self.documents: dict[str, str] = {}
        self.metas: dict[str, dict] = {}
        self.uris: dict[str, str] = {}
        self.get_calls: list[dict | None] = []             # where= 호출 로그(하위호환)
        self.get_by_ids_calls: list[tuple[list[str], list[str] | None]] = []
        self.delete_calls: list[list[str]] = []
        self.update_calls: list[tuple[list[str], list[dict]]] = []
        self.upsert_calls: list[dict] = []
        self.add_calls: list[dict] = []
        # 결함 주입(기본값: 무해) — docstring 참고
        self.fail_get_calls: set[int] = set()
        self.fail_delete_ids: set[str] = set()
        self.fail_add_ids: set[str] = set()
        self.lossy_add_ids: set[str] = set()
        self._get_by_ids_call_count = 0

    def seed(self, node_id, pack_id="pack-1", embedding=(0.1, 0.2, 0.3),
             document="본문", metadata=None, uri=None):
        """편의 헬퍼 — id 하나에 4축(embedding/document/metadata/uri)을 한 번에 채운다."""
        self._rows[node_id] = pack_id
        self.embeddings[node_id] = list(embedding)
        self.documents[node_id] = document
        self.metas[node_id] = dict(metadata) if metadata is not None else {}
        if uri is not None:
            self.uris[node_id] = uri
        return node_id

    def get(self, ids=None, where=None, include=None):
        if ids is not None:
            self._get_by_ids_call_count += 1
            self.get_by_ids_calls.append((list(ids), list(include) if include else None))
            if self._get_by_ids_call_count in self.fail_get_calls:
                raise RuntimeError(f"simulated get failure (call #{self._get_by_ids_call_count})")
            found = [i for i in ids if i in self._rows]
            out: dict = {"ids": found}
            inc = include or []
            if "embeddings" in inc:
                out["embeddings"] = [self.embeddings.get(i) for i in found]
            if "documents" in inc:
                out["documents"] = [self.documents.get(i) for i in found]
            if "metadatas" in inc:
                out["metadatas"] = [self.metas.get(i, {}) for i in found]
            if "uris" in inc:
                out["uris"] = [self.uris.get(i) for i in found]
            return out
        # where= 호출 (delete_pack/pack_live_counts/live_pack_state 기존 계약)
        self.get_calls.append(where)
        pid = (where or {}).get("pack_id")
        ids_ = [i for i, p in self._rows.items() if p == pid]
        return {"ids": ids_}

    def delete(self, ids):
        if any(i in self.fail_delete_ids for i in ids):
            raise RuntimeError("simulated delete failure")   # 상태 변형 전에 던진다
        self.delete_calls.append(list(ids))
        for i in ids:
            self._rows.pop(i, None)
            self.embeddings.pop(i, None)
            self.documents.pop(i, None)
            self.metas.pop(i, None)
            self.uris.pop(i, None)

    def add(self, ids, embeddings=None, documents=None, metadatas=None, uris=None):
        self.add_calls.append({"ids": list(ids)})            # 실패해도 시도는 기록
        if any(i in self.fail_add_ids for i in ids):
            raise RuntimeError("simulated add failure")
        for idx, i in enumerate(ids):
            if i in self.lossy_add_ids:
                # "성공"하지만 메타만 쓴다 — 레코드 자체는 존재하되(ids 는 조회되되)
                # 임베딩·문서는 비워 둔 채(v11 검수가 잡은 lossy add 재현).
                self._rows.setdefault(i, "pack-1")
                if metadatas is not None:
                    self.metas[i] = dict(metadatas[idx])
                continue
            self._rows.setdefault(i, "pack-1")
            if embeddings is not None:
                self.embeddings[i] = list(embeddings[idx])
            if documents is not None:
                self.documents[i] = documents[idx]
            if metadatas is not None:
                self.metas[i] = dict(metadatas[idx])          # add 는 치환 — 새로 짓는다
            if uris is not None:
                self.uris[i] = uris[idx]

    def update(self, ids, metadatas):
        self.update_calls.append((list(ids), list(metadatas)))
        for i, m in zip(ids, metadatas):
            if i not in self._rows:            # 실측: 부재 id 는 예외 없이 no-op
                continue
            self.metas.setdefault(i, {}).update(m)            # 병합

    def upsert(self, ids, embeddings=None, documents=None, metadatas=None, uris=None):
        self.upsert_calls.append({"ids": list(ids)})
        for idx, i in enumerate(ids):
            self._rows.setdefault(i, "pack-1")
            if embeddings is not None:
                self.embeddings[i] = list(embeddings[idx])
            if documents is not None:
                self.documents[i] = documents[idx]
            if metadatas is not None:
                self.metas.setdefault(i, {}).update(metadatas[idx])   # 병합
            if uris is not None:
                self.uris[i] = uris[idx]
            # uris 를 안 주면 기존 uri 를 손대지 않는다 — 보존 실측.


class _FakeChromaVec:
    """`_conn`/`conn`/`_engine` 이 없고 `_collection` 만 있는 Chroma 형태."""

    available = True

    def __init__(self, rows):
        self._collection = _FakeChromaCollection(rows)

    def upsert_texts(self, texts, ids=None, metadatas=None):
        """실 `ChromaVecStore.upsert_texts` 위임 계약을 그대로 흉내낸다 — 임베딩은
        안 넘기고(컬렉션이 문서에서 자동 계산하는 자리) documents/metadatas/ids 만
        `.upsert()` 로 넘긴다. `load_chunks_incremental` 의 재임베딩 폴백 경로가
        이 메서드를 통해서만 벡터스토어에 닿으므로, R1 통합 게이트(⑫⑭)가 이 메서드를
        거친다."""
        self._collection.upsert(ids=ids, documents=list(texts), metadatas=metadatas)
        return list(ids) if ids is not None else []


class TestChromaBackendBranches:
    """`_vec_backend()` 가 `"chroma"` 로 인식하는 형태(F5-1) — 4자리 전부를 태운다."""

    def test_delete_pack_uses_single_pack_id_predicate_and_deletes_the_matched_rows(
            self, live, tmp_path):
        _builder, graph, docs = live
        vec = _FakeChromaVec({"a1": "pack-a", "a2": "pack-a", "b1": "pack-b"})

        _n, _c, chunk_vec_del = pack_load.delete_pack("pack-a", graph, docs, vec)

        assert chunk_vec_del == 2, f"벡터 2건이 지워져야 한다 (실제 {chunk_vec_del})"
        assert vec._collection.get_calls[-1] == {"pack_id": "pack-a"}, (
            f"회수 술어가 pack_id 단일 조건이 아니다: {vec._collection.get_calls[-1]} — "
            "$or 로 되돌리면 이 단언이 깨져야 한다")
        assert set(vec._collection._rows) == {"b1"}, (
            f"다른 팩의 벡터까지 지웠거나 대상이 남았다: {vec._collection._rows}")

    def test_pack_live_counts_counts_via_get(self, tmp_path):
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
        graph = LocalGraphStore(str(tmp_path / "graph.db"))
        docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
        vec = _FakeChromaVec({"a1": "pack-a", "a2": "pack-a", "b1": "pack-b"})

        got = pack_load.pack_live_counts("pack-a", graph, docs, vec)
        assert got["vectors"] == 2, f"벡터 2건을 세어야 한다 (실제 {got['vectors']!r})"
        assert vec._collection.get_calls[-1] == {"pack_id": "pack-a"}

    def test_live_pack_state_collects_vec_ids_via_get(self, live, tmp_path):
        builder, graph, docs = live
        self._seed_node(builder, tmp_path, "n1")
        vec = _FakeChromaVec({"n1": "pack-1", "고아": "pack-1", "b1": "pack-b"})

        state = pack_load.live_pack_state("pack-1", graph, docs, vec)
        assert state["vec_ids"] == {"n1", "고아"}, state["vec_ids"]
        assert vec._collection.get_calls[-1] == {"pack_id": "pack-1"}

    def _seed_node(self, builder, tmp_path, node_id):
        f = _write_jsonl(tmp_path / f"{node_id}.jsonl", [_node(id=node_id)])
        pack_load.load_nodes("pack-1", f, builder, {})


class TestVecMetaUpdateChromaReplace:
    """`_vec_meta_update` 의 chroma 분기 — get→delete→add 치환 + 4축 후검증(R1, 2026-08-13).

    2026-08-13 이전엔 이 분기가 `handle.update(ids=..., metadatas=...)` 하나였다
    (`TestChromaBackendBranches.test_vec_meta_update_calls_collection_update`,
    이 클래스로 대체). chromadb 의 `update` 는 **병합**이라 스키마가 줄어든 메타의
    스테일 키를 못 지웠다(실측, `_FakeChromaCollection` docstring) — 그래서
    delete+add 치환으로 바꿨고, 그 대가로 늘어난 실패 표면(get 2회·delete·add)을
    아래에서 개별로 건다. 번호는 v18 설계의 게이트 번호와 대응한다.
    """

    # ① 부재 → False
    def test_missing_record_returns_false(self):
        vec = _FakeChromaVec({})
        ok = pack_load._vec_meta_update(vec, "ghost", {"a": 1})
        assert ok is False
        assert vec._collection.delete_calls == []
        assert vec._collection.add_calls == []

    # ② 존재+스테일 키 → True + 메타 정확 일치(스테일 소멸)
    def test_replaces_and_drops_stale_metadata_keys(self):
        vec = _FakeChromaVec({})
        vec._collection.seed("c1", embedding=[0.1, 0.2, 0.3], document="본문",
                              metadata={"stale": "old", "쪽": "1"})
        ok = pack_load._vec_meta_update(vec, "c1", {"쪽": "99"})
        assert ok is True
        assert vec._collection.metas["c1"] == {"쪽": "99"}, (
            "치환이 아니라 병합이면 'stale' 키가 살아남는다")
        assert vec._collection.delete_calls == [["c1"]]
        assert vec._collection.add_calls[-1]["ids"] == ["c1"]

    # ③ 임베딩 보존(+ 허용오차 값 비교 — v16 검수)
    def test_embedding_is_preserved_exactly(self):
        vec = _FakeChromaVec({})
        emb = [0.11111, -0.22222, 0.33333]
        vec._collection.seed("c1", embedding=emb, document="본문", metadata={"a": 1})
        ok = pack_load._vec_meta_update(vec, "c1", {"a": 2})
        assert ok is True
        assert vec._collection.embeddings["c1"] == emb

    def test_embedding_within_float_tolerance_still_passes(self):
        """float32 왕복 오차 수준(허용오차 1e-6 rel+abs)은 실패로 잡지 않는다."""
        vec = _FakeChromaVec({})
        emb = [1.0, 2.0, 3.0]
        vec._collection.seed("c1", embedding=emb, document="본문", metadata={"a": 1})
        real_add = vec._collection.add

        def add_with_epsilon(ids, embeddings=None, documents=None, metadatas=None, uris=None):
            if embeddings is not None:
                embeddings = [[v + 1e-9 for v in row] for row in embeddings]
            return real_add(ids, embeddings, documents, metadatas, uris)
        vec._collection.add = add_with_epsilon

        ok = pack_load._vec_meta_update(vec, "c1", {"a": 2})
        assert ok is True, "허용오차 이내 부동소수 미세오차까지 실패로 잡았다"

    def test_embedding_value_drift_beyond_tolerance_fails_post_check(self):
        """차원은 같지만 **값**이 달라지면 후검증이 잡는다(v16 검수 결함 재발 방지).

        차원만 보는 후검증이었다면 이 케이스가 조용히 통과했다 — 임베딩 전체가
        바뀌어도 True 가 나왔을 것이다.
        """
        vec = _FakeChromaVec({})
        vec._collection.seed("c1", embedding=[1.0, 2.0, 3.0], document="본문", metadata={"a": 1})
        real_add = vec._collection.add

        def add_with_wrong_values(ids, embeddings=None, documents=None, metadatas=None, uris=None):
            if embeddings is not None:
                embeddings = [[v + 1.0 for v in row] for row in embeddings]  # 값 자체가 다르다
            return real_add(ids, embeddings, documents, metadatas, uris)
        vec._collection.add = add_with_wrong_values

        ok = pack_load._vec_meta_update(vec, "c1", {"a": 2})
        assert ok is False, "임베딩 값이 전부 바뀌었는데 후검증을 통과했다"

    # ⑥ URI 레코드 → delete 미호출 + False + warning + (fake upsert 후) URI 보존
    def test_uri_record_is_not_replaced_and_returns_false(self, caplog):
        vec = _FakeChromaVec({})
        vec._collection.seed("c1", embedding=[0.1, 0.2], document="본문",
                              metadata={"y": "1"}, uri="http://example.com/c1")
        with caplog.at_level("WARNING"):
            ok = pack_load._vec_meta_update(vec, "c1", {"y": "99"})
        assert ok is False
        assert vec._collection.delete_calls == [], "URI 레코드인데 delete 가 불렸다"
        assert vec._collection.add_calls == []
        assert any("URI" in r.message for r in caplog.records), "URI 우회 경고가 안 남았다"

    def test_uri_is_preserved_when_caller_falls_back_to_upsert(self):
        """False 를 받은 호출자가 실제로 재임베딩(`upsert_texts`)으로 우회하면
        URI 는 보존되고 메타는 병합된다 — `load_chunks_incremental` 전체 경로로 확인."""
        vec = _FakeChromaVec({})
        old_row = _chunk_row("c1", "본문A", y="1")
        new_row = _chunk_row("c1", "본문A", y="99")
        old_meta = transform_chunk_meta("pack-1", old_row)
        vec._collection.seed("c1", embedding=[0.1, 0.2], document="본문A",
                              metadata=old_meta, uri="http://example.com/c1")
        live_chunks = {"c1": ("본문A", old_meta)}
        chunks_file = _write_jsonl_chunks_tmp([new_row])

        stats = pack_load.load_chunks_incremental(
            "pack-1", chunks_file, vec, _NullDocs(), live_chunks)
        c_new, c_txt, c_meta, c_same, err, bypack_ids = stats

        assert err == 0, "재임베딩 우회 경로에서 오류가 났다"
        assert c_txt == 1, "재임베딩(텍스트) 경로로 카운트돼야 한다"
        assert vec._collection.delete_calls == [], "URI 레코드는 delete 가 불리면 안 된다"
        assert vec._collection.uris.get("c1") == "http://example.com/c1", "URI 가 사라졌다"

    # ⑦ delete 실패 fake → False(예외 비전파) + warning
    def test_delete_failure_returns_false_without_propagating(self, caplog):
        vec = _FakeChromaVec({})
        vec._collection.seed("c1", embedding=[0.1], document="본문", metadata={"a": 1})
        vec._collection.fail_delete_ids = {"c1"}
        with caplog.at_level("WARNING"):
            ok = pack_load._vec_meta_update(vec, "c1", {"a": 2})  # 예외가 새면 여기서 죽는다
        assert ok is False
        assert vec._collection.add_calls == [], "delete 가 실패했는데 add 를 시도했다"
        assert "c1" in vec._collection._rows, "delete 실패 후에도 원본이 남아 있어야 한다"
        assert any("치환 실패" in r.message for r in caplog.records)

    # ⑧ add 실패 → 복구 add 미호출(부재 유지) + False
    def test_add_failure_leaves_record_absent_without_recovery_add(self, caplog):
        vec = _FakeChromaVec({})
        vec._collection.seed("c1", embedding=[0.1], document="본문", metadata={"a": 1})
        vec._collection.fail_add_ids = {"c1"}
        with caplog.at_level("WARNING"):
            ok = pack_load._vec_meta_update(vec, "c1", {"a": 2})
        assert ok is False
        assert vec._collection.delete_calls == [["c1"]], "delete 는 실제로 실행돼야 한다"
        assert len(vec._collection.add_calls) == 1, (
            "add 실패 후 복구용 재-add 를 시도했다 — 설계는 '부재 유지'를 요구한다")
        assert "c1" not in vec._collection._rows, "add 실패 후 레코드가 부재 상태로 남아야 한다"

    # ⑩ add 무동작(lossy 포함) fake → 후검증 False
    def test_lossy_add_that_drops_embedding_fails_post_check(self):
        """add 가 예외 없이 '성공'하지만 메타만 쓰고 임베딩·문서를 비우면
        (v11 검수가 잡은 lossy add) 후검증이 잡아야 한다."""
        vec = _FakeChromaVec({})
        vec._collection.seed("c1", embedding=[0.1, 0.2], document="본문", metadata={"a": 1})
        vec._collection.lossy_add_ids = {"c1"}
        ok = pack_load._vec_meta_update(vec, "c1", {"a": 2})
        assert ok is False, "메타만 남기고 임베딩을 비운 lossy add 를 후검증이 못 잡았다"
        # lossy 경로라도 메타 자체는 반영됐을 수 있다 — 그래도 함수는 False 를 내야
        # 호출자가 재임베딩으로 우회해 임베딩 손실을 복구한다.
        assert vec._collection.embeddings.get("c1") is None

    # ⑫ add 실패 후 호출자 재임베딩 경로 — 최종 메타 정확 일치(스테일 무잔존)
    def test_add_failure_then_caller_reembed_leaves_no_stale_keys(self):
        """add 가 실패하면 delete 로 이미 레코드가 지워진 상태다(⑧) — 호출자가
        재임베딩(upsert_texts)으로 우회하면 그 upsert 는 **부재 위에서** 실행되므로
        병합할 옛 메타가 없다. 최종 메타가 새 메타와 정확히 같아야 한다(스테일 무잔존).
        """
        vec = _FakeChromaVec({})
        old_row = _chunk_row("c1", "본문A", stale="old", x="1")
        new_row = _chunk_row("c1", "본문A", x="99")   # 텍스트 불변, 메타만 변경(stale 제거)
        old_meta = transform_chunk_meta("pack-1", old_row)
        new_meta = transform_chunk_meta("pack-1", new_row)
        vec._collection.seed("c1", embedding=[0.1], document="본문A", metadata=old_meta)
        vec._collection.fail_add_ids = {"c1"}
        live_chunks = {"c1": ("본문A", old_meta)}
        chunks_file = _write_jsonl_chunks_tmp([new_row])

        stats = pack_load.load_chunks_incremental(
            "pack-1", chunks_file, vec, _NullDocs(), live_chunks)
        c_new, c_txt, c_meta, c_same, err, bypack_ids = stats

        assert err == 0
        assert c_txt == 1, "add 실패 → 재임베딩 경로로 떨어져야 한다"
        assert vec._collection.metas["c1"] == new_meta, (
            f"재임베딩 후에도 스테일 키가 남았다: {vec._collection.metas.get('c1')}")

    # ⑬ get(선·후) 예외 fake → False(비전파)
    def test_pre_get_exception_returns_false_without_propagating(self):
        vec = _FakeChromaVec({})
        vec._collection.seed("c1", embedding=[0.1], document="본문", metadata={"a": 1})
        vec._collection.fail_get_calls = {1}      # 선(존재확인) get
        ok = pack_load._vec_meta_update(vec, "c1", {"a": 2})  # 예외가 새면 여기서 죽는다
        assert ok is False
        assert vec._collection.delete_calls == [], "선-get 이 실패했는데 delete 로 진행했다"

    def test_post_get_exception_returns_false_without_propagating(self):
        vec = _FakeChromaVec({})
        vec._collection.seed("c1", embedding=[0.1], document="본문", metadata={"a": 1})
        vec._collection.fail_get_calls = {2}      # 후(검증) get
        ok = pack_load._vec_meta_update(vec, "c1", {"a": 2})  # 예외가 새면 여기서 죽는다
        assert ok is False
        assert vec._collection.delete_calls == [["c1"]], "후-get 실패 전까지는 delete+add 가 진행돼야 한다"

    # ⑭ delete 실패 통합 시나리오 — 겹치는 키 갱신 + 여분 스테일 키 잔존(#175 창)
    def test_delete_failure_then_caller_upsert_merges_and_leaves_stale_window(self):
        """delete 가 실패하면 레코드가 원본 그대로 남는다(⑦) — 호출자가 재임베딩
        (upsert_texts→upsert)으로 우회하면 그 upsert 는 **기존 레코드 위에서 병합**된다.
        겹치는 키("x")는 새 값으로 갱신되지만 스테일 키("stale")는 살아남는다 —
        이것이 localcrab#175 로 등록된, 아직 닫지 않은 창이다. 창의 존재와 정확한
        범위를 여기서 못박아 소리 없이 넓어지지 않게 한다.
        """
        vec = _FakeChromaVec({})
        old_row = _chunk_row("c1", "본문A", stale="old", x="1")
        new_row = _chunk_row("c1", "본문A", x="99")
        old_meta = transform_chunk_meta("pack-1", old_row)
        expected_meta = dict(old_meta, x="99")   # 겹치는 키 갱신 + stale 잔존(#175)
        vec._collection.seed("c1", embedding=[0.1], document="본문A", metadata=old_meta)
        vec._collection.fail_delete_ids = {"c1"}
        live_chunks = {"c1": ("본문A", old_meta)}
        chunks_file = _write_jsonl_chunks_tmp([new_row])

        stats = pack_load.load_chunks_incremental(
            "pack-1", chunks_file, vec, _NullDocs(), live_chunks)
        c_new, c_txt, c_meta, c_same, err, bypack_ids = stats

        assert err == 0
        assert c_txt == 1
        assert vec._collection.metas["c1"] == expected_meta, (
            f"#175 창의 정확한 모양이 바뀌었다(겹치는 키 갱신 + 스테일 잔존): "
            f"{vec._collection.metas.get('c1')}")


def _chunk_row(chunk_id, text, **metadata):
    """`load_chunks_incremental` 이 읽는 청크 행 하나. `metadata` 키워드는
    `transform_chunk_meta` 가 읽는 중첩 `metadata` 서브딕트로 들어간다 — 최상위에
    얹으면 무시된다(정본: `opencrab/pack/normalize.py:transform_chunk_meta`)."""
    return {"id": chunk_id, "text": text, "document_id": chunk_id, "metadata": metadata}


def _write_jsonl_chunks_tmp(rows):
    """`load_chunks_incremental` 용 임시 chunks.jsonl — `_chunk_row` 로 지은 행들을 쓴다."""
    import tempfile
    d = pathlib.Path(tempfile.mkdtemp(prefix="vecmeta_"))
    f = d / "chunks.jsonl"
    return _write_jsonl(f, rows)


class _NullDocs:
    """`docs.upsert_source` 만 흉내내는 무해 더블 — `load_chunks_incremental` 은
    이 호출을 `try/except: pass` 로 감싸므로 존재만 하면 충분하다(R1 통합 게이트는
    벡터 축만 본다)."""

    def upsert_source(self, *a, **kw):
        pass


class _SqlAlchemyVecLike:
    """pgvector 형태 흉내 — `_engine`/`_table` 만 노출한다(실 SQLAlchemy 엔진, in-memory
    SQLite 위에서 돈다).

    `_vec_backend()` 는 `_conn`/`conn` 도 `_collection` 도 없고 `_engine` 만 있는
    이 형태를 `"sqlalchemy"` 로 인식해야 한다 — pgvector 실스토어가 실제로
    `_engine`/`_table` 만 노출한다(load.py `_vec_backend` docstring).
    `begin()`/`connect()` 컨텍스트와 `rowcount`/`scalar()` 를 손으로 흉내내는 대신
    이미 프로젝트 의존성인 진짜 SQLAlchemy 를 그대로 쓴다 — 계약을 잘못 베낄
    위험이 없다.
    """

    _table = "vectors_pg"

    def __init__(self):
        from sqlalchemy import create_engine, text
        self.available = True
        self._engine = create_engine("sqlite:///:memory:")
        with self._engine.begin() as conn:
            conn.execute(text(
                f"CREATE TABLE {self._table} (node_id TEXT PRIMARY KEY, pack_id TEXT)"))

    def seed(self, pack, ids):
        from sqlalchemy import text
        with self._engine.begin() as conn:
            for i in ids:
                conn.execute(text(f"INSERT INTO {self._table} VALUES (:i, :p)"),
                             {"i": i, "p": pack})

    def rows(self):
        from sqlalchemy import text
        with self._engine.connect() as conn:
            return {(r[0], r[1]) for r in conn.execute(
                text(f"SELECT node_id, pack_id FROM {self._table}"))}


class TestSqlAlchemyBackendBranches:
    """`_vec_backend()` 가 `"sqlalchemy"` 로 인식하는 형태(pgvector, F5-1)."""

    def test_delete_pack_deletes_via_begin_and_reflects_real_rowcount(self, live, tmp_path):
        _builder, graph, docs = live
        vec = _SqlAlchemyVecLike()
        vec.seed("pack-a", ["a1", "a2"])
        vec.seed("pack-b", ["b1"])

        _n, _c, chunk_vec_del = pack_load.delete_pack("pack-a", graph, docs, vec)

        assert chunk_vec_del == 2, f"벡터 2건이 지워져야 한다 (실제 {chunk_vec_del})"
        assert vec.rows() == {("b1", "pack-b")}, (
            f"다른 팩의 벡터까지 지웠거나 대상이 남았다: {vec.rows()}")

    def test_pack_live_counts_counts_via_connect_and_scalar(self, tmp_path):
        from opencrab.stores.local_graph_store import LocalGraphStore
        from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
        graph = LocalGraphStore(str(tmp_path / "graph.db"))
        docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
        vec = _SqlAlchemyVecLike()
        vec.seed("pack-a", ["a1", "a2"])

        got = pack_load.pack_live_counts("pack-a", graph, docs, vec)
        assert got["vectors"] == 2, f"벡터 2건을 세어야 한다 (실제 {got['vectors']!r})"

    def test_live_pack_state_collects_vec_ids_via_connect(self, live, tmp_path):
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, builder, {})
        vec = _SqlAlchemyVecLike()
        vec.seed("pack-1", ["n1", "고아"])
        vec.seed("pack-b", ["b1"])

        state = pack_load.live_pack_state("pack-1", graph, docs, vec)
        assert state["vec_ids"] == {"n1", "고아"}, state["vec_ids"]

    def test_vec_meta_update_has_no_dedicated_branch_and_falls_back_to_false(self):
        """`_vec_meta_update` 는 `"sql"`/`"chroma"` 두 종류만 분기하고 `"sqlalchemy"`
        분기가 없다 — pgvector 형태는 예외 없이 `False` 로 떨어져 호출자가
        재임베딩으로 우회한다. 분기가 새로 생기면 이 테스트가 깨져 알려준다."""
        vec = _SqlAlchemyVecLike()
        vec.seed("pack-1", ["c1"])
        ok = pack_load._vec_meta_update(vec, "c1", {"쪽": "99"})
        assert ok is False, (
            "sqlalchemy 형태에 메타 갱신 전용 분기가 새로 생겼다 — 이 테스트를 "
            "그 분기에 맞게 다시 써야 한다")


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
        """축 집합은 vec 백엔드 지원 여부와 무관하게 고정 — `vectors` 키는 항상 있다.

        값의 **타입**은 갈린다(아래 별도 테스트) — `None`과 `0`은 다른 사실이라
        여기서는 축 집합만 건다.
        """
        graph, docs, vec = self._stores(tmp_path)
        got = pack_load.pack_live_counts("아무팩", graph, docs, vec)
        assert set(got) == self.AXES, (
            f"축 집합이 달라졌다: {sorted(got)} — 축을 빼면 그 축의 결손이 영영 안 보인다")

    def test_graph_and_doc_axes_are_always_int(self, tmp_path):
        """nodes/edges/docs는 스토어가 항상 있으니 백엔드와 무관하게 `int`다."""
        graph, docs, vec = self._stores(tmp_path)
        got = pack_load.pack_live_counts("아무팩", graph, docs, vec)
        for axis in ("nodes", "edges", "docs"):
            assert isinstance(got[axis], int), f"{axis}가 int가 아니다: {got[axis]!r}"

    def test_vectors_axis_is_int_when_backend_supports_counting(self, tmp_path):
        """벡터 백엔드가 팩 단위 카운트를 낼 수 있으면(`sql` 종류) `vectors`도 `int`다.

        `_vec_backend()`가 `_conn`/`_table`을 가진 sqlite-vec 형태(`_SqliteVecLike`)를
        인식해 실제로 세야 한다 — 종전에는 `getattr(vec, "_conn")`을 직접 봐서
        Chroma·pgvector에서 늘 0이었는데, 그 버그가 이 스텁으로는 안 잡혔다
        (스텁이 sqlite-vec 형태라 우연히 통과했다). 여기서 명시적으로 int 계약을 건다.
        """
        graph, docs, _ = self._stores(tmp_path)
        vec = _SqliteVecLike()
        vec.seed("아무팩", ["v1", "v2"])
        got = pack_load.pack_live_counts("아무팩", graph, docs, vec)
        assert got["vectors"] == 2, f"벡터 2건을 세어야 한다 (실제 {got['vectors']!r})"

    def test_empty_store_graph_axes_are_zero_not_missing(self, tmp_path):
        """없는 팩이라도 nodes/edges/docs는 **0**이다. 키가 빠지면 호출자가 KeyError로 죽는다."""
        graph, docs, vec = self._stores(tmp_path)
        got = pack_load.pack_live_counts("없는팩", graph, docs, vec)
        for axis in ("nodes", "edges", "docs"):
            assert got[axis] == 0, f"{axis}가 0이 아니다: {got[axis]!r}"

    def test_empty_but_supported_vec_backend_is_zero(self, tmp_path):
        """벡터 백엔드가 카운트를 **낼 수 있는데** 팩이 없으면 `0`이다(`None`이 아니다).

        `0`(세어보니 없다)과 `None`(셀 방법이 없어 모른다)을 가르는 계약의 반쪽 —
        지원되는 백엔드에서 빈 결과까지 `None`으로 새면 "모른다"와 "없다"가 다시 섞인다.
        """
        graph, docs, _ = self._stores(tmp_path)
        vec = _SqliteVecLike()  # 아무것도 seed하지 않음 — 빈 스토어
        assert pack_load.pack_live_counts("없는팩", graph, docs, vec)["vectors"] == 0

    def test_vector_store_without_conn_yields_none_not_crash(self, tmp_path):
        """벡터 백엔드가 팩 단위 카운트를 **못 내는** 경우 — 죽지 말고 `None`이어야 한다.

        종전 계약은 이 경우도 `0`을 돌려줬다. 그러면 "벡터가 없다"(사실)와
        "셀 방법을 모른다"(진단 정보 없음)가 같은 숫자로 뭉개져, 예컨대 pgvector
        미가용 팩과 실제로 벡터가 0건인 팩을 3원 대사 출력만 보고는 구분할 수 없었다.
        `_vec_backend()`가 인식하지 못하는 형태(`available` 속성조차 없음)는 `None`이다.
        """
        graph, docs, _ = self._stores(tmp_path)

        class NoConn:
            pass

        assert pack_load.pack_live_counts("p", graph, docs, NoConn())["vectors"] is None

    def test_the_sql_lives_here_only(self):
        """호출자가 같은 쿼리를 다시 선언하지 않는가 — **구조로** 건다.

        문서에 "정본은 하나"라고 적는 것으로는 두 벌이 되는 것을 못 막는다. 실제로
        그렇게 두 벌이 됐다. 이 모듈 안에 카운트 SQL 이 있는지만 확인하고, 호출자 쪽은
        그 리포의 게이트가 본다(단방향 의존이라 여기서 호출자를 알 수 없다).
        """
        sql = pack_load.COUNT_SQL
        assert set(sql) == {"nodes", "edges", "docs"}, (
            f"COUNT_SQL 축이 달라졌다: {sorted(sql)} — 벡터는 백엔드마다 테이블명이 달라 "
            "여기 두지 않는다(함수가 런타임에 정한다)")
        for axis, table in (("nodes", "graph_nodes"), ("edges", "graph_edges"),
                            ("docs", "doc_sources")):
            assert f"FROM {table}" in sql[axis], f"{axis} 쿼리가 {table} 를 안 센다"

        # **`docs` 는 두 태그 형태를 다 세야 한다.** `$.source` 절이 빠진 사본이
        # 실제로 있었고, 그 사본은 source 로만 태그된 행을 통째로 놓쳤다
        # (실측: 5건 중 3건 누락, 2026-08-11 적대 검증).
        assert "$.pack_id" in sql["docs"] and "$.source" in sql["docs"], (
            "docs 쿼리가 한쪽 태그만 센다 — 이것이 5벌 사본 중 하나에서 실제로 난 사고다")
        assert pack_load.COUNT_SQL_ARGC["docs"] == 2, (
            "docs 는 pack_name 을 두 번 받는다 — argc 가 틀리면 조용히 잘못 센다")

        # 함수가 그 문자열을 **실제로 쓰는가**. 상수만 맞고 본문이 딴 쿼리를 쓰면 무의미하다.
        import inspect

        body = inspect.getsource(pack_load.pack_live_counts)
        assert "COUNT_SQL[" in body, "정본 상수를 안 쓰고 쿼리를 다시 적었다"
        assert "FROM graph_nodes" not in body, "본문에 인라인 SQL 이 되살아났다"


class TestVectorMetadataFollowsDocMetadata:
    """텍스트 불변·메타만 변경일 때 **벡터 메타도 따라가는가**.

    안 따라가면 의미검색이 돌려주는 메타와 메타 필터가 옛 값을 계속 본다. 더 나쁜 것은
    **다음 증분이 갱신된 doc 메타와 비교해 "동일"로 판정**한다는 점이다 — 그 어긋남은
    전량 재적재 전에는 영영 안 고쳐진다(2026-08-11 리뷰 지적).

    미지원 백엔드에서는 **조용히 넘어가지 않고 재임베딩으로 우회**해야 한다.
    조용히 성공을 보고하면 같은 영구 불일치가 남는다.
    """

    def _pack(self, tmp_path, doc_id):
        return _write_jsonl(tmp_path / "c.jsonl", [
            {"id": "c1", "text": "고정된 본문", "document_id": doc_id, "source": "p"}])

    def test_metadata_change_reaches_the_vector_store(self, live, tmp_path):
        builder, graph, docs = live
        vec = _RecordingVec()
        pack_load.load_chunks("p", self._pack(tmp_path, "doc-A"), vec, docs)
        state = pack_load.live_pack_state("p", graph, docs, _NoVec())

        c_new, c_txt, c_meta, c_same, err, _ = pack_load.load_chunks_incremental(
            "p", self._pack(tmp_path, "doc-B"), vec, docs, state["chunks"])
        assert (c_meta, c_txt, c_same) == (1, 0, 0), (
            f"메타만 바뀐 청크가 meta 로 안 세어졌다 ({c_meta},{c_txt},{c_same})")
        assert vec.meta_updates, "벡터 메타 갱신이 호출되지 않았다 — doc 만 고쳐졌다"
        assert vec.metas["c1"]["document_id"] == "doc-B", (
            "벡터에 도달한 메타가 옛 값이다 — 의미검색이 계속 옛 메타를 돌려준다")

    def test_unsupported_backend_falls_back_to_re_embedding(self, live, tmp_path):
        """메타 갱신을 못 하는 백엔드면 **재임베딩으로라도** 맞춘다."""
        builder, graph, docs = live
        vec = _RecordingVec(supports_meta_update=False)
        pack_load.load_chunks("p", self._pack(tmp_path, "doc-A"), vec, docs)
        state = pack_load.live_pack_state("p", graph, docs, _NoVec())
        before = len(vec.ids)

        c_new, c_txt, c_meta, c_same, err, _ = pack_load.load_chunks_incremental(
            "p", self._pack(tmp_path, "doc-B"), vec, docs, state["chunks"])
        assert c_meta == 0 and c_txt == 1, (
            f"미지원 백엔드인데 meta 로 세었다 ({c_meta},{c_txt}) — 벡터가 옛 메타로 남는다")
        assert len(vec.ids) > before, "재임베딩이 안 일어났다"
        assert vec.metas["c1"]["document_id"] == "doc-B"


# ───────────────────────── 계약: 실패해도 조용히 성공하지 않는다 ─────────────────────────
#
# 아래 4개는 리뷰가 지목한 "실패 전파" 축이다 — 스토어 실패를 예외 없이 삼키는
# add_node/add_edge/delete_node 의 계약(반환값을 봐야 진짜 결과를 안다) 위에서,
# 실패가 (a) 카운터를 속이지 않는지 (b) 삭제 보호를 깨지 않는지 (c) 비교 기준을
# 섣불리 옮기지 않는지를 각각 건다.

class _VecMetaAlwaysFailsAndReembedAlsoFails:
    """`update_metadata`도, 재임베딩(`upsert_texts`)도 둘 다 실패 — 벡터가 통째로 죽은 상태.

    메타 전용 분기가 재임베딩으로 우회해도 그 우회조차 실패하면 벡터는 여전히 옛 값이다.
    이런 상황에서 `doc_sources`(다음 증분의 비교 기준)를 옮기면 다음 실행이 "동일"로
    오판해 이 불일치가 영구히 남는다 — 옮기지 않아야 한다.
    """

    available = True

    def update_metadata(self, chunk_id: str, meta: dict) -> bool:
        return False

    def upsert_texts(self, texts, metadatas=None, ids=None):
        raise RuntimeError("주입된 벡터 스토어 다운")

    def delete(self, ids):
        pass


class TestFailedVectorMetaUpdateDoesNotMoveTheDocBaseline:
    """① 벡터 갱신 실패 주입 시 doc 기준이 안 옮겨가고, 다음 증분이 c_same 이 아니어야 한다."""

    def test_doc_metadata_stays_old_and_next_increment_reprocesses(self, live, tmp_path):
        builder, graph, docs = live
        f1 = _write_jsonl(tmp_path / "c1.jsonl",
                          [{"id": "c1", "text": "고정된 본문", "document_id": "doc-A", "source": "p"}])
        pack_load.load_chunks("p", f1, _RecordingVec(), docs)
        state = pack_load.live_pack_state("p", graph, docs, _NoVec())

        f2 = _write_jsonl(tmp_path / "c2.jsonl",
                          [{"id": "c1", "text": "고정된 본문", "document_id": "doc-B", "source": "p"}])
        broken = _VecMetaAlwaysFailsAndReembedAlsoFails()
        c_new, c_txt, c_meta, c_same, err, _ = pack_load.load_chunks_incremental(
            "p", f2, broken, docs, state["chunks"])
        assert err == 1 and c_meta == 0, (
            f"벡터가 완전히 죽었는데 성공으로 세었다 (c_meta={c_meta} err={err})")

        row = docs._conn.execute(
            "SELECT metadata FROM doc_sources WHERE source_id=?", ("c1",)).fetchone()
        meta_after = json.loads(row[0])
        assert meta_after.get("document_id") == "doc-A", (
            "벡터 갱신이 실패했는데 doc 비교 기준이 새 메타로 옮겨갔다 — "
            f"다음 증분이 이 변경을 영영 못 본다 (실제 {meta_after.get('document_id')!r})")

        # 다음 증분: doc 이 옛 메타 그대로이므로 live[1] != meta 로 다시 걸려야 한다
        # (= c_same 이 아니어야 한다). 이번엔 정상 벡터로 재시도해 실제로 복구되는지도 본다.
        state2 = pack_load.live_pack_state("p", graph, docs, _NoVec())
        working_vec = _RecordingVec()
        c_new2, c_txt2, c_meta2, c_same2, err2, _ = pack_load.load_chunks_incremental(
            "p", f2, working_vec, docs, state2["chunks"])
        assert c_same2 == 0, (
            "doc 기준이 안 옮겨갔어야 하는데 다음 증분이 same 으로 판정했다 — 영구 불일치")
        assert working_vec.metas.get("c1", {}).get("document_id") == "doc-B", (
            "재시도에서도 벡터에 새 메타가 도달하지 않았다")


class TestFailedEdgeWriteStaysInAppliedProtection:
    """② 저장 실패 엣지가 `applied` 에 남아 `incremental_finalize` 의 stale 삭제를 면해야 한다.

    실패해도 안 넣으면, 이전 증분에서 이미 라이브에 들어가 있는 동일 엣지가
    `stale_edges = live_edges - applied_edges` 계산에 걸려 삭제된다
    (재현: live_edges={('f','r','t')}, applied_edges=set() → stale_delete_would_run=True,
    2026-08-11 적대 검증).
    """

    def test_store_write_failure_does_not_orphan_the_live_edge(self, live, tmp_path, monkeypatch):
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1"), _node(id="n2")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)

        ef = _write_jsonl(tmp_path / "edges.jsonl",
                          [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "CITES"}])
        ok, skip, err = pack_load.load_edges("pack-1", ef, builder, id_map)
        assert ok == 1, "사전 조건: 최초 적재는 성공해야 한다"

        live_state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert live_state["edges"], "사전 조건: 엣지가 라이브에 있어야 한다"

        # 이번 증분에서 같은 엣지를 다시 반영하려는데 그래프 쓰기가 실패한다고 가정.
        def _broken_upsert_edge(*a, **kw):
            raise RuntimeError("주입된 그래프 쓰기 실패")
        monkeypatch.setattr(graph, "upsert_edge", _broken_upsert_edge)

        applied: set = set()
        ok2, skip2, err2 = pack_load.load_edges(
            "pack-1", ef, builder, id_map, applied=applied)
        assert ok2 == 0 and err2 == 1, f"저장 실패가 err 로 안 잡혔다 (ok={ok2} err={err2})"
        assert applied, "저장 실패해도 applied 에는 넣어야 한다 — 안 넣으면 stale 로 오판된다"

        result = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), live_state,
            bypack_node_ids={"n1", "n2"}, bypack_chunk_ids=set(),
            applied_edges=applied, force_delete=False,
            nodes_total=2, chunks_total=0,
        )
        assert result["edge_del"] == 0, (
            "applied 에 남은 실패 엣지가 stale 로 오판돼 지워졌다")
        left = graph._conn.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE from_id=?", ("n1",)).fetchone()[0]
        assert left == 1, f"라이브 엣지가 실제로 삭제됐다 (남은 {left}건)"


class TestFailedAddNodeLeavesOldTypedRowIntact:
    """③ 타입 변경에서 `add_node` 저장이 실패하면 구 타입 행이 살아남아야 한다.

    새 행이 실제로 저장된 뒤에만 구 타입 행을 지우는 순서(load.py 주석 참조)가
    지켜지지 않으면, 저장 실패 시 구 행과 그 cascade 엣지가 이미 사라진 채로
    다음 증분도 같은 이유로 또 실패해 **영구 소실**된다.
    """

    def test_add_node_store_failure_keeps_the_old_type(self, live, tmp_path, monkeypatch):
        builder, graph, docs = live
        f_old = _write_jsonl(tmp_path / "old.jsonl", [_node(id="n1", node_type="Document")])
        pack_load.load_nodes("pack-1", f_old, builder, {})
        assert graph.get_node("Document", "n1") is not None, "사전 조건: 구 타입 행이 있어야 한다"

        live_nodes = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())["nodes"]

        def _broken_upsert_node(*a, **kw):
            raise RuntimeError("주입된 그래프 쓰기 실패")
        monkeypatch.setattr(graph, "upsert_node", _broken_upsert_node)

        f_new = _write_jsonl(tmp_path / "new.jsonl",
                             [_node(id="n1", node_type="Concept", space="concept")])
        n_new, n_chg, n_same, skip, err, _ids = pack_load.load_nodes_incremental(
            "pack-1", f_new, builder, {}, live_nodes, graph, docs, {})

        assert err == 1, (
            f"저장 실패가 err 로 안 잡혔다 (n_new={n_new} n_chg={n_chg} err={err})")
        assert graph.get_node("Document", "n1") is not None, (
            "add_node 가 실패했는데 구 타입 행이 지워졌다 — 재시도로도 복구 불가능한 영구 소실")
        assert graph.get_node("Concept", "n1") is None, "실패했는데 신규 타입 행이 생겼다"


class TestDocSpaceResidueCleanup:
    """F4 — doc 고아 세 부류(타입 변경 잔재 / 입력에서 사라진 고아 / space 어긋남)를
    각각 확인한다(2026-08-11 F4 지시).
    """

    def test_type_change_residue_is_cleaned_even_on_the_same_path(self, live, tmp_path):
        """(a) graph 는 새 타입, doc 은 구 space. 노드가 입력에 아직 있고 same 으로
        끝나는 경로에서도 구 space doc 행이 지워져야 한다."""
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl",
                          [_node(id="n1", node_type="Concept", space="concept")])
        pack_load.load_nodes("pack-1", nf, builder, {})
        assert graph.get_node("Concept", "n1") is not None

        # 구 space 잔재를 직접 심는다 — 지난 증분의 F4-c 정리가 실패했다고 가정한다.
        docs.upsert_node_doc("resource", "Document", "n1", {"pack_id": "pack-1"})

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert state["doc_node_spaces"].get("n1") == {"concept", "resource"}, (
            "전제: doc_node_spaces 가 두 space 를 다 모아야 한다")

        # 같은 파일을 다시 적재 — 노드 자체는 안 바뀌었으므로 same 경로를 타야 한다.
        n_new, n_chg, n_same, skip, err, _ids = pack_load.load_nodes_incremental(
            "pack-1", nf, builder, {}, state["nodes"], graph, docs, state["doc_node_spaces"])
        assert n_same == 1, f"전제 위반 — same 경로가 아니다: new={n_new} chg={n_chg} same={n_same}"

        left_spaces = {r[0] for r in docs._conn.execute(
            "SELECT space FROM doc_nodes WHERE node_id=?", ("n1",))}
        assert left_spaces == {"concept"}, (
            f"same 경로에서 구 space(resource) doc 잔재가 안 지워졌다: {left_spaces}")

    def test_orphan_doc_node_without_graph_twin_is_cleaned_by_finalize(self, live, tmp_path):
        """(b) graph 행 없음, doc 행만 남음. `incremental_finalize` 의 doc 축이 잡아야 한다."""
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", nf, builder, {})
        docs.upsert_node_doc("resource", "Document", "orphan-1", {"pack_id": "pack-1"})

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert "orphan-1" not in state["nodes"], "전제: graph 트윈이 없어야 한다"
        assert state["doc_node_spaces"].get("orphan-1") == {"resource"}

        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n1"}, set(), set(), True, 1, 0)

        assert res["doc_orphan_del"] == 1, res
        left = docs._conn.execute(
            "SELECT COUNT(*) FROM doc_nodes WHERE node_id=?", ("orphan-1",)).fetchone()[0]
        assert left == 0, "graph 트윈 없는 doc 고아가 안 지워졌다"

    def test_all_spaces_for_a_doc_candidate_are_removed_not_just_the_live_space(
            self, live, tmp_path):
        """(c) 삭제 후보 노드의 doc 행이 여러 space 에 걸쳐 있으면 **전부** 지워져야
        한다 — `live_nodes` 의 space 하나만 지우면 다른 space 잔재가 남는다."""
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1"), _node(id="n2")])
        pack_load.load_nodes("pack-1", nf, builder, {})
        docs.upsert_node_doc("concept", "Concept", "n1", {"pack_id": "pack-1"})  # 어긋난 space 잔재

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert state["doc_node_spaces"]["n1"] == {"resource", "concept"}
        assert state["nodes"]["n1"][1] == "resource", "전제: graph 축 space 는 resource 뿐이다"

        res = pack_load.incremental_finalize(   # n1 이 by-pack 에서 사라졌다고 신고
            "pack-1", graph, docs, _NoVec(), state,
            {"n2"}, set(), set(), True, 1, 0)

        left_spaces = {r[0] for r in docs._conn.execute(
            "SELECT space FROM doc_nodes WHERE node_id=?", ("n1",))}
        assert left_spaces == set(), (
            f"삭제 후보 노드의 doc 행이 live space(resource) 만 지워지고 다른 "
            f"space(concept) 잔재가 남았다: {left_spaces}, res={res}")

    def test_space_moving_type_change_cleans_stale_and_legacy_spaces_together(
            self, live, tmp_path):
        """(d, v10 검수: 실제 F4 잔재를 대표하는 시나리오) — 타입이 바뀌면서 space 도
        함께 바뀐다. live 는 구 타입(구 space) 하나, doc 은 구 space 행 + 무관
        legacy space 행 2종. 입력이 신 타입(신 space)으로 들어오면 stale_typed
        삭제(구 space, load.py:604)와 `_cleanup_stale_doc_spaces`(legacy space,
        load.py:610)가 **함께** 걸려야 신·구·legacy 세 space 전부가 맞는 최종
        상태로 수렴한다. 후자 하나만 빠져도(예: chg 경로 호출 제거) legacy 행이
        남는다.
        """
        builder, graph, docs = live
        nf1 = _write_jsonl(tmp_path / "n1.jsonl",
                           [_node(id="n1", node_type="Document", space="resource")])
        pack_load.load_nodes("pack-1", nf1, builder, {})
        assert graph.get_node("Document", "n1") is not None

        # legacy space 잔재 — 지난 증분이 F4-c 정리 전에 실패했다고 가정한다.
        docs.upsert_node_doc("subject", "Agent", "n1", {"pack_id": "pack-1"})

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert state["doc_node_spaces"].get("n1") == {"resource", "subject"}, (
            "전제: doc_node_spaces 가 구 space·legacy space 를 모두 모아야 한다")

        nf2 = _write_jsonl(tmp_path / "n2.jsonl",
                           [_node(id="n1", node_type="Concept", space="concept")])
        n_new, n_chg, n_same, skip, err, _ids = pack_load.load_nodes_incremental(
            "pack-1", nf2, builder, {}, state["nodes"], graph, docs, state["doc_node_spaces"])
        assert (n_new, n_chg, n_same, skip, err) == (0, 1, 0, 0, 0), (
            n_new, n_chg, n_same, skip, err)

        assert graph.get_node("Document", "n1") is None, "구 타입 그래프 행이 안 지워졌다"
        assert graph.get_node("Concept", "n1") is not None, "신 타입 그래프 행이 없다"

        left_spaces = {r[0] for r in docs._conn.execute(
            "SELECT space FROM doc_nodes WHERE node_id=?", ("n1",))}
        assert left_spaces == {"concept"}, (
            f"구 space(resource) · legacy space(subject) 잔재가 안 지워지고 남았다: "
            f"{left_spaces}")

    def test_same_space_sequential_type_change_updates_the_single_row_in_place(
            self, live, tmp_path):
        """같은 space 안에서 타입만 바뀌는 순차 변경 — `doc_nodes` PK 가
        `(space, node_id)` 라 물리 행은 **하나**로 유지돼야 하고(UPSERT 갱신),
        그 하나가 최종 타입·properties 값을 반영해야 한다. 행 수만 보면 안
        갈린다(v10 조건) — 갱신을 빼먹고 구 값을 그대로 둬도 행 수는 1 그대로다.

        이관 회귀 회귀방지(2026-08-12): stale_typed doc 삭제에 space 동일성 가드가
        없으면(load.py:604 부근) space 가 안 바뀐 이 경로에서 `add_node` 가 upsert 로
        방금 갱신한 행을 그대로 지워 물리 행이 **0개**가 된다(실측·team-lead 확인 완료).
        """
        builder, graph, docs = live
        nf1 = _write_jsonl(tmp_path / "n1.jsonl",
                           [_node(id="n1", node_type="Document", space="resource")])
        pack_load.load_nodes("pack-1", nf1, builder, {})
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())

        nf2 = _write_jsonl(tmp_path / "n2.jsonl",
                           [_node(id="n1", node_type="File", space="resource",
                                  properties={"버전": "2"})])
        n_new, n_chg, n_same, skip, err, _ids = pack_load.load_nodes_incremental(
            "pack-1", nf2, builder, {}, state["nodes"], graph, docs, state["doc_node_spaces"])
        assert (n_new, n_chg, n_same, skip, err) == (0, 1, 0, 0, 0), (
            n_new, n_chg, n_same, skip, err)

        rows = docs._conn.execute(
            "SELECT node_type, properties FROM doc_nodes WHERE space=? AND node_id=?",
            ("resource", "n1")).fetchall()
        assert len(rows) == 1, f"같은 space 순차 타입 변경인데 물리 행이 {len(rows)}개다"
        node_type, props_json = rows[0]
        props = json.loads(props_json)
        assert node_type == "File", f"doc_nodes 행의 node_type 이 신 타입으로 갱신 안 됨: {node_type}"
        assert props.get("버전") == "2", f"doc_nodes 행의 properties 가 신 값으로 갱신 안 됨: {props}"


class TestAnchorPredicateAgreesAcrossPythonAndSQL:
    """`_is_anchor_node`(Python)와 `ANCHOR_SQL`(SQLite GLOB)이 **같은 판정**을 내야 한다.

    `LIKE` 였다면 대소문자를 무시해 `DATASET:x` 도 앵커로 잡고 Python `str.startswith`
    는 안 잡아 두 축이 갈렸을 것이다(load.py 주석). `GLOB` 은 대소문자를 구분해
    일치한다 — 그 일치를 대소문자 격자로 확인한다.
    """

    @pytest.mark.parametrize("node_id, expect_anchor", [
        ("dataset:x", True), ("DATASET:x", False), ("DaTaSeT:x", False),
    ])
    def test_python_predicate_is_case_sensitive(self, node_id, expect_anchor):
        assert pack_load._is_anchor_node(node_id, {}) is expect_anchor

    @pytest.mark.parametrize("node_id, expect_anchor", [
        ("dataset:x", True), ("DATASET:x", False), ("DaTaSeT:x", False),
    ])
    def test_sql_doc_axis_agrees_with_the_python_predicate(
            self, live, tmp_path, node_id, expect_anchor):
        """앵커면 `doc_node_spaces` 에서 **빠져야** 하고, 아니면 **있어야** 한다 —
        SQL(GLOB) 판정이 Python 술어와 갈리면 이 단언이 깨진다."""
        builder, graph, docs = live
        docs.upsert_node_doc("resource", "Document", node_id, {"pack_id": "pack-1"})
        doc_node_spaces = pack_load.live_pack_state(
            "pack-1", graph, docs, _NoVec())["doc_node_spaces"]
        is_excluded = node_id not in doc_node_spaces
        assert is_excluded == expect_anchor, (
            f"{node_id}: SQL(GLOB) 이 앵커로 판정({is_excluded})했지만 Python 술어는 "
            f"{pack_load._is_anchor_node(node_id, {})} — ANCHOR_SQL 과 _is_anchor_node 가 불일치")

    def test_title_backfill_anchor_is_excluded_from_the_doc_axis_too(self, live, tmp_path):
        """`created_by=title-backfill` 도 `dataset:` 접두사와 같은 자격으로 doc 축에서
        빠져야 한다 — 지금까지 doc 축(SQL) 쪽으로는 무테스트였다."""
        builder, graph, docs = live
        docs.upsert_node_doc("resource", "Document", "backfilled-1",
                              {"pack_id": "pack-1", "created_by": "title-backfill"})
        doc_node_spaces = pack_load.live_pack_state(
            "pack-1", graph, docs, _NoVec())["doc_node_spaces"]
        assert "backfilled-1" not in doc_node_spaces, (
            "title-backfill 앵커가 doc_node_spaces 후보에 들어왔다 — SQL 축이 안 걸렀다")

    def test_row_without_created_by_key_is_not_silently_excluded(self, live, tmp_path):
        """`created_by` 키가 아예 없는 정상 행. `COALESCE` 가 없으면 `json_extract` 가
        NULL 을 내 `NULL = 'title-backfill'` 도 NULL 이 되고, `NOT (... OR NULL)` 이
        WHERE 절에서 falsy 로 취급돼 이 행이 앵커가 아닌데도 doc_node_spaces 에서
        통째로 빠진다 — 그러면 이런 행은 doc 축 대사에서 영영 제외된다."""
        builder, graph, docs = live
        docs.upsert_node_doc("resource", "Document", "n1", {"pack_id": "pack-1"})
        doc_node_spaces = pack_load.live_pack_state(
            "pack-1", graph, docs, _NoVec())["doc_node_spaces"]
        assert "n1" in doc_node_spaces, (
            "created_by 가 없는 정상 행이 doc_node_spaces 에서 빠졌다 — COALESCE 누락 의심")


class _FalsyButNonEmptyDict(dict):
    """`bool()` 은 항상 False 를 내지만 `set(...)`/순회는 실제 키를 낸다.

    `doc_del_candidates = set(doc_node_spaces) - bypack_node_ids` 는 `doc_node_spaces`
    에서 파생되므로, 정상 상태로는 "분모가 비었는데 후보가 있다"는 불변식 위반이
    나올 수 없다(집합 뺄셈은 부분집합만 낸다) — 그래서 이 안전핀은 `doc_node_spaces`
    계산 자체가 깨진 미래의 버그를 잡기 위한 방어선이다. 그 방어선이 실제로
    발동하는지는 `not doc_node_spaces`(bool)와 `set(doc_node_spaces)`(iteration)가
    서로 다른 정보를 보는, 진성이 아닌 입력으로만 확인할 수 있다.
    """

    def __bool__(self):
        return False


class TestDocAxisSafetyPinEdgeCases:
    def test_empty_doc_node_spaces_denominator_does_not_raise_zero_division(
            self, live, tmp_path):
        """doc_node_spaces 가 비면 30% 핀을 건너뛰어야 한다 — 분모가 0인 나눗셈을
        시도하면 ZeroDivisionError 로 죽는다."""
        builder, graph, docs = live
        state = pack_load.live_pack_state("없는-팩", graph, docs, _NoVec())
        assert state["doc_node_spaces"] == {} and not state["nodes"] and not state["chunks"]

        res = pack_load.incremental_finalize(   # 예외 없이 끝나야 한다
            "없는-팩", graph, docs, _NoVec(), state,
            set(), set(), set(), False, 0, 0)
        assert res["doc_orphan_del"] == 0

    def test_invariant_violation_pin_fires_when_candidates_outrun_the_denominator(
            self, live, tmp_path):
        """비었는데(bool 기준) 후보가 있으면(순회 기준) 불변식 위반으로 중단해야 한다.

        `doc_del_candidates` 가 `doc_node_spaces` 의 부분집합이라는 불변식은 정상
        입력으로는 절대 안 깨진다 — `_FalsyButNonEmptyDict` 로 `not doc_node_spaces`
        와 `set(doc_node_spaces)` 를 인위적으로 갈라놓아야 이 핀이 발동한다.
        """
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", nf, builder, {})
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        state = dict(state)
        state["doc_node_spaces"] = _FalsyButNonEmptyDict({"n1": {"resource"}})

        with pytest.raises(SystemExit) as ei:
            pack_load.incremental_finalize(   # bypack 은 비지 않게(0-item 핀 회피), n1 은 안 담아 후보로 남긴다
                "pack-1", graph, docs, _NoVec(), state,
                {"다른-노드"}, set(), set(), False, 1, 0)
        assert "불변식 위반" in str(ei.value), str(ei.value)


class TestDocAxisDenominatorAndMutationGuards:
    """G5 — doc 30% 핀 분모(노드 수 vs 행 수) 픽스처 + 별건 변형 3건 겨냥 테스트.

    분모 픽스처는 노드 축·doc 축의 분모를 **의도적으로 다르게** 만들어(노드 10개
    중 3개만 doc_node_spaces 에 남기고 그 3개에 총 5행을 분산) 같은 후보 집합이
    노드 수 분모로는 33.3%(발동)를, 행 수 분모로는 20%(미발동)를 낸다 — 이 조합은
    `incremental_finalize` 가 분모를 노드 수 대신 행 수로 재는 변이를 그대로 잡는다
    (`SystemExit` 를 기대하는데 변이 하에서는 안 나므로 즉시 red).
    """

    def _seed_nodes(self, builder, tmp_path, ids, pack="pack-1"):
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id=i) for i in ids])
        pack_load.load_nodes(pack, f, builder, {})

    def test_doc_axis_denominator_is_node_count_and_fires_at_thirty_three_percent(
            self, live, tmp_path):
        builder, graph, docs = live
        ids = [f"n{i}" for i in range(10)]
        self._seed_nodes(builder, tmp_path, ids)
        for nid in ids[3:]:                     # 7개는 doc 행을 지워 노드 분모를 3으로 좁힌다
            docs.delete_node_doc("resource", nid)
        # n1·n2 에 legacy space 행을 더해 물리 행 합을 5로 벌린다(노드 분모 3과 다른 값) —
        # 행 수 분모였다면 1/5=20%(미발동)가 됐을 조합이다.
        docs.upsert_node_doc("subject", "Agent", "n1", {"pack_id": "pack-1"})
        docs.upsert_node_doc("subject", "Agent", "n2", {"pack_id": "pack-1"})

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert set(state["doc_node_spaces"]) == {"n0", "n1", "n2"}, (
            f"전제: doc_node_spaces 가 3개 노드여야 한다: {set(state['doc_node_spaces'])}")
        total_rows = sum(len(v) for v in state["doc_node_spaces"].values())
        assert total_rows == 5, f"전제: 물리 행 합이 5여야 한다(노드 분모 3과 달라야 함): {total_rows}"

        keep = set(ids) - {"n0"}                # 노드·doc 후보 둘 다 n0 하나뿐
        node_ratio = 1 / 10
        chunk_ratio = 0 / max(1, len(state["chunks"]))
        assert node_ratio < 0.30 and chunk_ratio < 0.30, (
            "전제(자기 단언): 노드·청크 축은 30% 미만이어야 doc 축만 걸린다")

        with pytest.raises(SystemExit) as ei:
            pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                keep, set(), set(), False, len(keep), 0)
        assert str(ei.value) == _expect_doc_ratio_msg("pack-1", 1, 3), str(ei.value)

    def test_doc_axis_exactly_thirty_percent_with_asymmetric_denominator_is_not_aborted(
            self, live, tmp_path):
        """doc 축 분모(10)가 노드 축 분모(20)와 **다른 채로** 정확히 0.30 경계를
        걸어야, `>` 를 `>=` 로 바꾸는 변이와 분모를 다른 값으로 바꾸는 변이를
        동시에 잡는다. 타 축(<30%) 도 함께 자기 단언한다."""
        builder, graph, docs = live
        ids = [f"n{i}" for i in range(20)]
        self._seed_nodes(builder, tmp_path, ids)
        for nid in ids[10:]:                    # 10개는 doc 행을 지워 doc 분모를 10으로 좁힌다
            docs.delete_node_doc("resource", nid)

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert set(state["doc_node_spaces"]) == set(ids[:10]), (
            f"전제: doc_node_spaces 가 10개 노드여야 한다: {set(state['doc_node_spaces'])}")

        candidates = {"n0", "n1", "n2"}          # doc 분모 10 중 3 = 정확히 0.30
        keep = set(ids) - candidates
        node_ratio = len(candidates) / 20
        chunk_ratio = 0 / max(1, len(state["chunks"]))
        assert node_ratio < 0.30, f"전제(자기 단언): 노드 축은 30% 미만이어야 한다: {node_ratio}"
        assert chunk_ratio < 0.30

        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            keep, set(), set(), False, len(keep), 0)
        assert res["doc_orphan_del"] == 3, (
            f"doc 축 정확히 30%(분모 10)는 핀에 걸리면 안 된다: {res}")

    def test_load_nodes_incremental_doc_node_spaces_has_no_default(self):
        """`doc_node_spaces` 는 필수 인자다(기본값이 없다) — 기본값 `None` 이 되살아나면
        호출자가 안 넘겨도 예외 없이 F4-c 정리가 조용히 꺼진다(load.py 독스트링,
        "한동안 기본값 None 으로 받아 생략을 허용했는데...조용히 꺼진다")."""
        params = inspect.signature(pack_load.load_nodes_incremental).parameters
        assert params["doc_node_spaces"].default is inspect.Parameter.empty, (
            "doc_node_spaces 에 기본값이 생겼다 — 필수 인자 계약이 깨졌다")

    def test_doc_node_spaces_reconcile_predicate_is_pack_id_only_not_four_key(
            self, live, tmp_path):
        """`live_pack_state` 의 doc_node_spaces 대사(reconcile) 술어는 `pack_id`
        단일 키다(F4-b) — `delete_pack` 의 회수(4키: pack_id/source/source_id/pack)
        와 폭이 다르다. 4키로 넓히면 `pack` 으로만 태그된 행(pack_id 없음)이 후보에
        섞여, 그래프는 안 바뀌었는데 doc 만 지워지는 새 비대칭이 생긴다."""
        builder, graph, docs = live
        docs.upsert_node_doc("resource", "Document", "n-legacy-pack-tag-only",
                              {"pack": "pack-1"})   # pack_id 없음, legacy pack 필드만
        doc_node_spaces = pack_load.live_pack_state(
            "pack-1", graph, docs, _NoVec())["doc_node_spaces"]
        assert "n-legacy-pack-tag-only" not in doc_node_spaces, (
            "legacy pack 필드만 있는 doc 행이 doc_node_spaces 후보에 들어왔다 — "
            "대사 술어가 pack_id 단일 키에서 4키로 넓어진 것으로 의심된다")


class TestDocOrphanDeleteFalseIsNotCounted:
    def test_delete_node_doc_returning_false_does_not_increment_doc_orphan_del(
            self, live, tmp_path, monkeypatch):
        """`delete_node_doc` 이 실제로는 못 지웠다는 뜻인 `False` 를 돌려주면
        `doc_orphan_del` 이 오르면 안 된다 — 그래프 축의 `delete_node`/`False` 계약
        (`TestDeleteNodeFalseIsNotCounted`)과 같은 요구를 doc 축에도 건다."""
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", nf, builder, {})
        docs.upsert_node_doc("resource", "Document", "orphan-1", {"pack_id": "pack-1"})
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert "orphan-1" in state["doc_node_spaces"]

        monkeypatch.setattr(docs, "delete_node_doc", lambda *a, **kw: False)

        res = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            {"n1"}, set(), set(), True, 1, 0)
        assert res["doc_orphan_del"] == 0, (
            "delete_node_doc 가 False 를 돌려줬는데 doc_orphan_del 을 세었다 — "
            f"실제로는 안 지워졌는데 지웠다고 보고한다 (res={res})")


class TestDeleteNodeFalseIsNotCounted:
    """④ `delete_node` 가 `False`(실제로는 안 지워짐)를 돌려주면 `node_del` 이 오르면 안 된다.

    `_sql_graph_base.py` 의 4백엔드 통일 계약("Returns True iff the node itself was
    deleted")을 무시하고 예외 없이 통과했다는 이유만으로 세면, 실제로는 아무것도 안
    지워졌는데 "지웠다"고 보고하는 상태가 만들어진다.
    """

    def test_incremental_finalize_node_del_reflects_actual_deletion(
            self, live, tmp_path, monkeypatch):
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, builder, {})
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert "n1" in state["nodes"], "사전 조건: 노드가 라이브에 있어야 한다"

        monkeypatch.setattr(graph, "delete_node", lambda *a, **kw: False)

        # by-pack 에 n1 대신 다른 id 만 있다고 신고 — n1 이 삭제 후보가 되지만
        # delete_node 가 False 를 돌려준다. bypack_node_ids 를 완전히 비우면
        # 안전핀 0(by-pack 파일 누락 의심)이 먼저 sys.exit 하므로 빈 집합은 피한다.
        result = pack_load.incremental_finalize(
            "pack-1", graph, docs, _NoVec(), state,
            bypack_node_ids={"다른-노드"}, bypack_chunk_ids=set(),
            applied_edges=set(), force_delete=True,
            nodes_total=0, chunks_total=0,
        )
        assert result["node_del"] == 0, (
            "delete_node 가 False 를 돌려줬는데 node_del 을 세었다 — "
            f"실제로는 안 지워졌는데 지웠다고 보고한다 (node_del={result['node_del']})")

    def test_delete_pack_node_del_reflects_actual_deletion(self, live, tmp_path, monkeypatch):
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", nf, builder, {})
        assert graph.get_node("Document", "n1") is not None

        monkeypatch.setattr(graph, "delete_node", lambda *a, **kw: False)
        node_del, _chunk_sql_del, _chunk_vec_del = pack_load.delete_pack(
            "pack-1", graph, docs, _NoVec())

        assert node_del == 0, (
            "delete_node 가 False 를 돌려줬는데 delete_pack 의 node_del 을 세었다 "
            f"(실제 {node_del})")


class TestLoadLogsInsteadOfSwallowing:
    """G4-7 — load.py 안 두 곳의 무로그 삼킴을 `log.warning` 으로 바꿨다(예외는
    여전히 흡수해 적재를 안 죽이지만, 이제 기록은 남는다). `caplog` 로 주입한
    예외 메시지가 실제로 로그에 도달하는지 확인한다.
    """

    def test_stale_typed_doc_delete_failure_is_logged_not_silently_swallowed(
            self, live, tmp_path, monkeypatch, caplog):
        """`load_nodes_incremental` 의 타입 변경 구 doc 삭제(load.py:604)가 실패하면
        예전엔 `except Exception: pass` 로 조용히 삼켰다 — 이제 `log.warning` 이다.

        `doc_node_spaces={}` 로 넘겨 F4-c(`_cleanup_stale_doc_spaces`)의 자체
        `delete_node_doc` 호출을 비활성화하고, 이 지점(stale_typed 정리) 하나만
        격리한다(이 파일의 기존 관례 — `TestFailedAddNodeLeavesOldTypedRowIntact` 도
        마지막 인자로 `{}` 를 쓴다).
        """
        builder, graph, docs = live
        nf1 = _write_jsonl(tmp_path / "n1.jsonl",
                           [_node(id="n1", node_type="Document", space="resource")])
        pack_load.load_nodes("pack-1", nf1, builder, {})
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())

        def _boom(*a, **kw):
            raise RuntimeError("주입된 doc 삭제 실패")
        monkeypatch.setattr(docs, "delete_node_doc", _boom)

        nf2 = _write_jsonl(tmp_path / "n2.jsonl",
                           [_node(id="n1", node_type="Concept", space="concept")])
        with caplog.at_level("WARNING", logger="opencrab.pack.load"):
            n_new, n_chg, n_same, skip, err, _ids = pack_load.load_nodes_incremental(
                "pack-1", nf2, builder, {}, state["nodes"], graph, docs, {})
        assert (n_new, n_chg, n_same, skip, err) == (0, 1, 0, 0, 0), (
            n_new, n_chg, n_same, skip, err)
        assert any("주입된 doc 삭제 실패" in r.getMessage() for r in caplog.records), (
            "타입 변경 구 doc 삭제 실패가 로그에 안 남았다: "
            f"{[r.getMessage() for r in caplog.records]}")

    def test_node_deletion_failure_in_incremental_finalize_is_logged_and_continues(
            self, live, tmp_path, monkeypatch, caplog):
        """`incremental_finalize` 의 노드 삭제 루프(load.py 약 1031행)가 예외를
        던지면 예전엔 `except Exception: deleted = False` 로 조용히 삼켰다 —
        이제 `log.warning` 을 남기고 다음 노드로 계속한다(삭제는 여전히 실패로
        취급 — `node_del` 이 오르면 안 된다)."""
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1"), _node(id="n2")])
        pack_load.load_nodes("pack-1", nf, builder, {})
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())

        def _boom(*a, **kw):
            raise RuntimeError("주입된 노드 삭제 실패")
        monkeypatch.setattr(graph, "delete_node", _boom)

        with caplog.at_level("WARNING", logger="opencrab.pack.load"):
            res = pack_load.incremental_finalize(
                "pack-1", graph, docs, _NoVec(), state,
                {"무관-id"}, set(), set(), True, 0, 0)
        assert res["node_del"] == 0, (
            "노드 삭제가 예외를 던졌는데 node_del 을 세었다 — "
            f"실제로는 안 지워졌는데 지웠다고 보고한다: {res}")
        assert any("주입된 노드 삭제 실패" in r.getMessage() for r in caplog.records), (
            "노드 삭제 실패가 로그에 안 남았다: "
            f"{[r.getMessage() for r in caplog.records]}")
