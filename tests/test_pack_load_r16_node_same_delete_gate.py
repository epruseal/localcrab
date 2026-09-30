"""r16 - #424: 노드 증분 적재의 same 경로가 팩 소유권 인가 없이 삭제한다.

`load_nodes_incremental`은 same 판정 행에서 `builder.add_node()`를 부르지
않는다. 그래서 그 경로의 문서 공간 잔재 정리(`_cleanup_stale_doc_spaces`)와
루프 뒤 구 타입 행 스윕은 팩 소유권 `authorize()`를 한 번도 거치지 않았다.
벡터 축이 없는 배포(`vec=None`)에서는 진입부 인가도 없었다. 이 파일은
"진입부에서 무조건 인가한다"는 계약을 고정한다.

픽스처는 `tests/test_pack_load.py`의 `live`와 `pack_sql`을 재사용한다.
"""
from __future__ import annotations

import pytest

from opencrab.auth import Principal, create_user, principal_scope
from opencrab.pack import load as pack_load
from opencrab.pack.ownership import PackNotFoundError
from tests.test_pack_load import (  # noqa: F401 - 기존 픽스처 재사용
    _LIVE_TEST_USER,
    _node,
    _NoVec,
    _write_jsonl,
    live,
    pack_sql,
)


def _spy(monkeypatch, obj, name):
    """`obj.name` 호출을 기록하고 원 메서드에 위임한다."""
    calls: list[tuple] = []
    real = getattr(obj, name)

    def _wrapped(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(obj, name, _wrapped)
    return calls


def _same_path_with_stale_doc_space(live, tmp_path):
    """그래프는 concept, doc 은 concept + 옛 resource 잔재. 같은 파일을 다시
    넣으면 same 경로가 잔재 정리에 닿는다."""
    builder, graph, docs = live
    nf = _write_jsonl(tmp_path / "n.jsonl",
                      [_node(id="n1", node_type="Concept", space="concept")])
    assert pack_load.load_nodes("pack-1", nf, builder, {}) == (1, 0, 0)
    docs.upsert_node_doc("resource", "Document", "n1", {"pack_id": "pack-1"})
    state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
    assert state["doc_node_spaces"].get("n1") == {"concept", "resource"}, "전제 위반"
    return builder, graph, docs, nf, state


def _left_spaces(docs):
    return {r[0] for r in docs._conn.execute(
        "SELECT space FROM doc_nodes WHERE node_id=?", ("n1",))}


def _intruder(pack_sql):
    uid = create_user(pack_sql, "node-same-intruder", is_local=False)
    return Principal(user_id=uid, is_local=False, disabled=False)


class TestSamePathDeleteNeedsOwnership:
    def test_non_owner_same_path_makes_zero_delete_calls(
            self, live, tmp_path, monkeypatch, pack_sql):
        builder, graph, docs, nf, state = _same_path_with_stale_doc_space(live, tmp_path)
        doc_del = _spy(monkeypatch, docs, "delete_node_doc")
        graph_del = _spy(monkeypatch, graph, "delete_node")

        with principal_scope(_intruder(pack_sql)):
            with pytest.raises(PackNotFoundError):
                pack_load.load_nodes_incremental(
                    "pack-1", nf, builder, {}, state["nodes"], graph, docs,
                    state["doc_node_spaces"], sql=pack_sql)

        assert doc_del == [], f"비소유 principal 인데 문서 삭제가 호출됐다: {doc_del}"
        assert graph_del == [], f"비소유 principal 인데 그래프 삭제가 호출됐다: {graph_del}"
        assert _left_spaces(docs) == {"concept", "resource"}, "잔재가 지워졌다"

    def test_owner_control_same_path_still_cleans_the_residue(
            self, live, tmp_path, monkeypatch, pack_sql):
        builder, graph, docs, nf, state = _same_path_with_stale_doc_space(live, tmp_path)
        doc_del = _spy(monkeypatch, docs, "delete_node_doc")

        n_new, n_chg, n_same, skip, err, _ids, _vu = pack_load.load_nodes_incremental(
            "pack-1", nf, builder, {}, state["nodes"], graph, docs,
            state["doc_node_spaces"], sql=pack_sql)

        assert (n_new, n_chg, n_same, skip, err) == (0, 0, 1, 0, 0), "same 경로 전제 위반"
        assert doc_del == [("resource", "n1")], doc_del
        assert _left_spaces(docs) == {"concept"}

    def test_unbound_principal_makes_zero_delete_calls(
            self, live, tmp_path, monkeypatch, pack_sql):
        builder, graph, docs, nf, state = _same_path_with_stale_doc_space(live, tmp_path)
        doc_del = _spy(monkeypatch, docs, "delete_node_doc")

        # `live` 픽스처가 연 principal_scope 를 중첩 스레드 없이 벗길 수 없으므로
        # 미바인딩은 로더의 바인딩 조회 자체를 끊어 재현한다.
        def _unbound():
            raise LookupError

        monkeypatch.setattr("opencrab.auth.current_principal", _unbound)
        with pytest.raises(RuntimeError, match="principal_scope"):
            pack_load.load_nodes_incremental(
                "pack-1", nf, builder, {}, state["nodes"], graph, docs,
                state["doc_node_spaces"], sql=pack_sql)

        assert doc_del == []
        assert _left_spaces(docs) == {"concept", "resource"}

    def test_missing_sql_raises_value_error_even_without_vec(
            self, live, tmp_path, monkeypatch):
        builder, graph, docs, nf, state = _same_path_with_stale_doc_space(live, tmp_path)
        doc_del = _spy(monkeypatch, docs, "delete_node_doc")

        with pytest.raises(ValueError, match="sql.*필수"):
            pack_load.load_nodes_incremental(
                "pack-1", nf, builder, {}, state["nodes"], graph, docs,
                state["doc_node_spaces"])

        assert doc_del == []
        assert _left_spaces(docs) == {"concept", "resource"}


class TestEmptyInputStillGated:
    def test_owner_with_empty_file_returns_zeros(self, live, tmp_path, pack_sql):
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "empty.jsonl", [])
        n_new, n_chg, n_same, skip, err, ids, _vu = pack_load.load_nodes_incremental(
            "pack-1", nf, builder, {}, {}, graph, docs, {}, sql=pack_sql)
        assert (n_new, n_chg, n_same, skip, err, ids) == (0, 0, 0, 0, 0, set())

    def test_non_owner_with_empty_file_is_rejected(self, live, tmp_path, pack_sql):
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "empty.jsonl", [])
        with principal_scope(_intruder(pack_sql)):
            with pytest.raises(PackNotFoundError):
                pack_load.load_nodes_incremental(
                    "pack-1", nf, builder, {}, {}, graph, docs, {}, sql=pack_sql)


class TestLegacyTypeSweepNeedsOwnership:
    """SQL 그래프는 node_id 가 기본 키라 실제로는 구 타입 중복 행이 안 생긴다.
    스윕은 레거시 행 전용이므로 조회 결과만 주입하고 삭제 호출을 관측한다."""

    @staticmethod
    def _inject_legacy_dup(monkeypatch):
        monkeypatch.setattr(
            pack_load, "_dup_type_node_rows",
            lambda graph, pack_name: {"n1": {"Concept", "Legacy"}})

    def _all_same(self, live, tmp_path):
        builder, graph, docs = live
        nf = _write_jsonl(tmp_path / "n.jsonl",
                          [_node(id="n1", node_type="Concept", space="concept")])
        assert pack_load.load_nodes("pack-1", nf, builder, {}) == (1, 0, 0)
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        return builder, graph, docs, nf, state

    def test_owner_control_sweep_attempts_the_delete(
            self, live, tmp_path, monkeypatch, pack_sql):
        builder, graph, docs, nf, state = self._all_same(live, tmp_path)
        self._inject_legacy_dup(monkeypatch)
        graph_del = _spy(monkeypatch, graph, "delete_node")

        res = pack_load.load_nodes_incremental(
            "pack-1", nf, builder, {}, state["nodes"], graph, docs,
            state["doc_node_spaces"], sql=pack_sql)

        assert res[2] == 1, "same 경로 전제 위반"
        assert graph_del == [("Legacy", "n1")], graph_del

    def test_non_owner_sweep_makes_zero_delete_calls(
            self, live, tmp_path, monkeypatch, pack_sql):
        builder, graph, docs, nf, state = self._all_same(live, tmp_path)
        self._inject_legacy_dup(monkeypatch)
        graph_del = _spy(monkeypatch, graph, "delete_node")

        with principal_scope(_intruder(pack_sql)):
            with pytest.raises(PackNotFoundError):
                pack_load.load_nodes_incremental(
                    "pack-1", nf, builder, {}, state["nodes"], graph, docs,
                    state["doc_node_spaces"], sql=pack_sql)

        assert graph_del == []
