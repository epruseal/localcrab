"""r17 - #425: 벡터 ID 열거가 일시 실패하면 증분 적재 전체가 멈춘다.

두 증분 적재 함수(청크 축, 노드 축)는 시작할 때 라이브 벡터 ID를 한 번
열거한다. 열거가 예외를 던지면 행 루프 전에 적재가 멈추고 그래프와 문서
정합화까지 막혔다. 계약: 백엔드 오류는 "벡터 상태 미확인"으로 접는다. 단건
조회로도 확인하지 못한 same 후보의 회수만 보류하고 미확인 카운트와 경고로
드러낸다. 인가 실패와 프로그래밍 오류는 삼키지 않는다.
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from opencrab.auth import Principal, create_user, principal_scope
from opencrab.pack import load as pack_load
from opencrab.pack.ownership import PackNotFoundError
from tests.test_pack_load import (  # noqa: F401 - 기존 픽스처 재사용
    _node,
    _write_jsonl,
    live,
    pack_sql,
)
from tests.test_pack_load_r12_selfheal_gates import (
    _chunk_row,
    _EnumerableVec,
    _live_chunks_from_docs,
)
from tests.test_pack_load_r15_node_vec_gates import _EnumerableVecWithLookup


class _FlakyConn:
    """열거 질의(`WHERE pack_id`)에만 예외를 던지고 나머지는 위임한다."""

    def __init__(self, conn, owner):
        self._real = conn
        self._owner = owner

    def execute(self, sql, *args, **kw):
        err = self._owner.enum_error
        if err is not None and "WHERE pack_id" in sql:
            self._owner.enum_attempts += 1
            raise err
        return self._real.execute(sql, *args, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _flaky(cls):
    """`cls`(_EnumerableVec 계열) 인스턴스의 열거만 실패시키는 서브클래스."""

    class _Flaky(cls):
        def __init__(self, pack_id="pack-1"):
            super().__init__(pack_id)
            self.enum_error = None
            self.enum_attempts = 0
            self._conn = _FlakyConn(self._conn, self)

    return _Flaky


_FlakyNodeVec = _flaky(_EnumerableVecWithLookup)
_FlakyChunkVec = _flaky(_EnumerableVec)
_CLOSED = sqlite3.ProgrammingError("Cannot operate on a closed database.")


def _enum_failure_warnings(caplog):
    """열거 실패 경고(헬퍼)와 요약 경고(적재 끝)를 각각 센다. 둘 다 WARNING 이다."""
    warns = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    helper = [m for m in warns if m.startswith("벡터 ID 열거 실패(")]
    summary = [m for m in warns
               if m.startswith("벡터 유실 회수 미확인(") and "열거 실패로" in m]
    return helper, summary


class TestNodeAxis:
    def _baseline(self, live, tmp_path, pack_sql, rows):
        builder, graph, docs = live
        vec0 = _EnumerableVecWithLookup("pack-1")
        builder._vec = vec0
        f0 = _write_jsonl(tmp_path / "n0.jsonl", [_node(id="n1")])
        id_map: dict = {}
        assert pack_load.load_nodes("pack-1", f0, builder, id_map) == (1, 0, 0)
        state = pack_load.live_pack_state("pack-1", graph, docs, vec0)
        f = _write_jsonl(tmp_path / "n.jsonl", rows)
        return builder, graph, docs, state, f, id_map

    def test_enum_failure_still_syncs_graph_and_docs_and_flags_unconfirmed(
            self, live, tmp_path, pack_sql, caplog):
        builder, graph, docs, state, f, id_map = self._baseline(
            live, tmp_path, pack_sql, [_node(id="n1"), _node(id="n2")])
        vec1 = _FlakyNodeVec("pack-1")
        vec1.enum_error = _CLOSED
        builder._vec = vec1
        with caplog.at_level(logging.WARNING):
            n_new, n_chg, n_same, skip, err, ids, vu = pack_load.load_nodes_incremental(
                "pack-1", f, builder, id_map, state["nodes"], graph, docs,
                state["doc_node_spaces"], vec=vec1, sql=pack_sql)
        assert vec1.enum_attempts == 1
        assert (n_new, n_chg, n_same, skip, err) == (1, 0, 1, 0, 0)
        assert ids == {"n1", "n2"}
        assert vu == 1, "미확인 same 후보가 카운트되지 않았다"
        assert vec1.rows() == {"n2"}, "신규 노드의 벡터 쓰기는 계속돼야 한다"
        helper, summary = _enum_failure_warnings(caplog)
        assert len(helper) == 1 and len(summary) == 1

    def test_next_run_with_healthy_enumeration_recovers_the_slot(
            self, live, tmp_path, pack_sql):
        builder, graph, docs, state, f, id_map = self._baseline(
            live, tmp_path, pack_sql, [_node(id="n1")])
        vec1 = _FlakyNodeVec("pack-1")  # n1 슬롯 유실, 열거는 정상
        builder._vec = vec1
        n_new, n_chg, n_same, skip, err, _ids, vu = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state["nodes"], graph, docs,
            state["doc_node_spaces"], vec=vec1, sql=pack_sql)
        assert (n_new, n_chg, n_same, skip, err, vu) == (0, 1, 0, 0, 0, 0)
        assert vec1.rows() == {"n1"}

    def test_failed_run_then_healthy_run_recovers_the_slot(
            self, live, tmp_path, pack_sql):
        builder, graph, docs, state, f, id_map = self._baseline(
            live, tmp_path, pack_sql, [_node(id="n1")])
        vec1 = _FlakyNodeVec("pack-1")  # n1 슬롯 유실
        vec1.enum_error = _CLOSED
        builder._vec = vec1
        r1 = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state["nodes"], graph, docs,
            state["doc_node_spaces"], vec=vec1, sql=pack_sql)
        assert (r1[0], r1[1], r1[2], r1[6]) == (0, 0, 1, 1)
        assert vec1.rows() == set(), "열거 실패 실행은 회수하지 않는다"
        vec1.enum_error = None  # 같은 백엔드가 복구됐다
        state2 = pack_load.live_pack_state("pack-1", graph, docs, vec1)
        r2 = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state2["nodes"], graph, docs,
            state2["doc_node_spaces"], vec=vec1, sql=pack_sql)
        assert (r2[0], r2[1], r2[2], r2[6]) == (0, 1, 0, 0)
        assert vec1.rows() == {"n1"}

    def test_opt_in_single_lookup_still_confirms_after_enum_failure(
            self, live, tmp_path, pack_sql):
        builder, graph, docs, state, f, id_map = self._baseline(
            live, tmp_path, pack_sql, [_node(id="n1")])
        vec1 = _FlakyNodeVec("pack-1")  # n1 슬롯 유실, 단건 조회는 동작
        vec1.enum_error = _CLOSED
        builder._vec = vec1
        r = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state["nodes"], graph, docs,
            state["doc_node_spaces"], vec=vec1, sql=pack_sql, recover_vectors=True)
        assert (r[0], r[1], r[2], r[6]) == (0, 1, 0, 0)
        assert vec1.rows() == {"n1"}

    def test_programming_error_in_enumeration_still_propagates(
            self, live, tmp_path, pack_sql):
        builder, graph, docs, state, f, id_map = self._baseline(
            live, tmp_path, pack_sql, [_node(id="n1")])
        vec1 = _FlakyNodeVec("pack-1")
        vec1.enum_error = TypeError("bug")
        builder._vec = vec1
        with pytest.raises(TypeError):
            pack_load.load_nodes_incremental(
                "pack-1", f, builder, id_map, state["nodes"], graph, docs,
                state["doc_node_spaces"], vec=vec1, sql=pack_sql)

    def test_authorization_failure_still_raises_before_enumeration(
            self, live, tmp_path, pack_sql):
        builder, graph, docs, state, f, id_map = self._baseline(
            live, tmp_path, pack_sql, [_node(id="n1")])
        vec1 = _FlakyNodeVec("pack-1")
        vec1.enum_error = _CLOSED
        builder._vec = vec1
        uid = create_user(pack_sql, "enum-intruder", is_local=False)
        with principal_scope(Principal(user_id=uid, is_local=False, disabled=False)):
            with pytest.raises(PackNotFoundError):
                pack_load.load_nodes_incremental(
                    "pack-1", f, builder, id_map, state["nodes"], graph, docs,
                    state["doc_node_spaces"], vec=vec1, sql=pack_sql)
        assert vec1.enum_attempts == 0


class TestChunkAxis:
    def test_enum_failure_still_syncs_docs_and_flags_unconfirmed(
            self, live, tmp_path, pack_sql, caplog):
        _b, _g, docs = live
        f0 = _write_jsonl(tmp_path / "c0.jsonl", [_chunk_row("c1")])
        pack_load.load_chunks("pack-1", f0, _EnumerableVec("pack-1"), docs, sql=pack_sql)
        live_chunks = _live_chunks_from_docs(docs)
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1"), _chunk_row("c2")])
        vec1 = _FlakyChunkVec("pack-1")
        vec1.enum_error = _CLOSED
        with caplog.at_level(logging.WARNING):
            c_new, c_txt, c_meta, c_same, err, ids, vu = pack_load.load_chunks_incremental(
                "pack-1", f, vec1, docs, live_chunks, sql=pack_sql)
        assert vec1.enum_attempts == 1
        assert (c_new, c_txt, c_meta, c_same, err) == (1, 0, 0, 1, 0)
        assert ids == {"c1", "c2"}
        assert vu == 1
        assert vec1.rows() == {"c2"}
        helper, summary = _enum_failure_warnings(caplog)
        assert len(helper) == 1 and len(summary) == 1

    def test_healthy_enumeration_control_still_recovers(self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        pack_load.load_chunks("pack-1", f, _EnumerableVec("pack-1"), docs, sql=pack_sql)
        live_chunks = _live_chunks_from_docs(docs)
        vec1 = _FlakyChunkVec("pack-1")
        c_new, c_txt, c_meta, c_same, err, _ids, vu = pack_load.load_chunks_incremental(
            "pack-1", f, vec1, docs, live_chunks, sql=pack_sql)
        assert (c_new, c_txt, c_meta, c_same, err, vu) == (0, 1, 0, 0, 0, 0)
        assert vec1.rows() == {"c1"}

    def test_programming_error_in_enumeration_still_propagates(
            self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        pack_load.load_chunks("pack-1", f, _EnumerableVec("pack-1"), docs, sql=pack_sql)
        live_chunks = _live_chunks_from_docs(docs)
        vec1 = _FlakyChunkVec("pack-1")
        vec1.enum_error = AttributeError("bug")
        with pytest.raises(AttributeError):
            pack_load.load_chunks_incremental(
                "pack-1", f, vec1, docs, live_chunks, sql=pack_sql)

    def test_authorization_failure_still_raises_before_enumeration(
            self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        pack_load.load_chunks("pack-1", f, _EnumerableVec("pack-1"), docs, sql=pack_sql)
        live_chunks = _live_chunks_from_docs(docs)
        vec1 = _FlakyChunkVec("pack-1")
        vec1.enum_error = _CLOSED
        uid = create_user(pack_sql, "enum-intruder-c", is_local=False)
        with principal_scope(Principal(user_id=uid, is_local=False, disabled=False)):
            with pytest.raises(PackNotFoundError):
                pack_load.load_chunks_incremental(
                    "pack-1", f, vec1, docs, live_chunks, sql=pack_sql)
        assert vec1.enum_attempts == 0


@pytest.mark.parametrize("exc", [PermissionError("denied"), LookupError("missing")])
class TestAuthorizationLikeErrorsFromTheBackendPropagate:
    """열거 단계에서 백엔드가 직접 던진 권한성 예외도 접지 않는다."""

    def test_node_axis(self, live, tmp_path, pack_sql, exc):
        builder, graph, docs, state, f, id_map = TestNodeAxis()._baseline(
            live, tmp_path, pack_sql, [_node(id="n1")])
        vec1 = _FlakyNodeVec("pack-1")
        vec1.enum_error = exc
        builder._vec = vec1
        with pytest.raises(type(exc)):
            pack_load.load_nodes_incremental(
                "pack-1", f, builder, id_map, state["nodes"], graph, docs,
                state["doc_node_spaces"], vec=vec1, sql=pack_sql)

    def test_chunk_axis(self, live, tmp_path, pack_sql, exc):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        pack_load.load_chunks("pack-1", f, _EnumerableVec("pack-1"), docs, sql=pack_sql)
        live_chunks = _live_chunks_from_docs(docs)
        vec1 = _FlakyChunkVec("pack-1")
        vec1.enum_error = exc
        with pytest.raises(type(exc)):
            pack_load.load_chunks_incremental(
                "pack-1", f, vec1, docs, live_chunks, sql=pack_sql)
