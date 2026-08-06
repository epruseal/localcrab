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
        builder, _, _ = live
        nf = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id="n1"), _node(id="n2")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", nf, builder, id_map)
        lower = _write_jsonl(tmp_path / "e_lower.jsonl",
                             [{"id": "e1", "source_id": "n1", "target_id": "n2", "label": "cites"}])
        upper = _write_jsonl(tmp_path / "e_upper.jsonl",
                             [{"id": "e2", "source_id": "n1", "target_id": "n2", "label": "CITES"}])
        assert pack_load.load_edges("p", lower, builder, id_map) == pack_load.load_edges("p", upper, builder, id_map)


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
        self._fail_over = fail_batches_larger_than

    def upsert_texts(self, texts, ids, metadatas):
        self.calls.append((len(ids), list(ids)))
        if self._fail_over is not None and len(ids) > self._fail_over:
            raise RuntimeError("주입된 배치 실패")
        self.ids.extend(ids)


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
