"""r15 — #377: 노드 축에 recover_vectors 회수 경로가 없다.

`load_chunks_incremental`은 청크 축에서 벡터 슬롯만 유실된 same-후보를 R1
(열거 가능 백엔드, #332/r12)과 opt-in 단건 조회(열거 불가 백엔드,
#332/r14)로 회수한다. `load_nodes_incremental`에는 이 짝이 없어서 그래프/
문서가 라이브와 같아도 벡터만 유실된 노드는 매 증분이 다시 n_same으로
떨어져 **영구히 회수되지 않았다** — 이 파일이 그 결손을 폐쇄하는 게이트다.

노드 축은 `write_gate.node_identity_conflict`가 `add_node`마다
`vector.get_by_id(node_id)`를 프로브하므로(§#197 슬롯 소유권 게이트와
별개), 여기 쓰는 더블은 전부 `get_by_id`를 구현해야 한다 — 없으면 그
프로브가 CONFLICT_UNVERIFIABLE로 회수 시도 자체를 거부해 이 파일이
검증하려는 경로에 진입조차 못 한다(설계 v1 §9, 스크래치 재현에서 먼저
확인됨).

기존 픽스처·더블은 `tests/test_pack_load.py`(진짜 SQLite 3스토어)와
`tests/test_pack_load_r12_selfheal_gates.py`(`_EnumerableVec`, kind=sql
인식 더블)와 `tests/test_pack_load_r14_unenumerable_vec_gates.py`
(`_UnenumerableVecWithLookup`, kind=None + 단건 조회 더블, [#197] 소유권
게이트 재사용)에서 재사용한다(네 번째 사본 방지).
"""
from __future__ import annotations

import logging
import pathlib
import sys

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.ontology.builder import OntologyBuilder
from opencrab.pack import load as pack_load
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from tests.test_pack_load import (  # noqa: F401 — 기존 픽스처·더블 재사용
    _LIVE_TEST_USER,
    _node,
    _write_jsonl,
    live,
    pack_sql,
)
from tests.test_pack_load_r12_selfheal_gates import _EnumerableVec
from tests.test_pack_load_r14_unenumerable_vec_gates import (
    _UnenumerableVecWithLookup,
)

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _vec_helpers import build_vector_store  # noqa: E402

# ───────────────────────── 노드 전용 더블 ─────────────────────────

class _EnumerableVecWithLookup(_EnumerableVec):
    """`_EnumerableVec`(kind=sql 인식, R1 열거 대상) + `get_by_id` —
    `add_node`의 `node_identity_conflict` 프로브가 요구한다(위 모듈
    docstring 참고). 순수 `_EnumerableVec`(r12, 청크 전용 — `load_chunks`는
    bulk 경로라 이 프로브를 안 탄다)는 이 메서드가 없어 노드 축에는 못
    쓴다."""

    def get_by_id(self, doc_id: str):
        row = self._conn.execute(
            f"SELECT node_id, pack_id FROM {self._table} WHERE node_id = ?",
            (doc_id,)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "document": "", "metadata": {"pack_id": row[1]}}


def _summary_records(caplog) -> list:
    """세 분기 요약 로그(``벡터 유실 회수 미확인(...)``)만 골라낸다 — 노드별
    즉시 경고(``벡터 유실 회수(...)``)와 섞이지 않게 한다."""
    return [r for r in caplog.records if "벡터 유실 회수 미확인(" in r.getMessage()]


# ───────────────────────── 게이트 ㉳ — R1(열거 가능) ─────────────────────────

class TestNodeVectorOnlyLossRecoveryEnumerable:
    """R1 — n_same 경로가 열거 가능 백엔드에서 벡터만 유실된 상태를
    회수해야 한다(recover_vectors 값과 무관하게 항상 검사)."""

    def test_vector_only_loss_is_recovered_as_chg_then_converges_to_same(
            self, live, tmp_path):
        builder, graph, docs = live
        vec0 = _EnumerableVecWithLookup("pack-1")
        builder._vec = vec0
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        id_map: dict = {}
        ok, skip, err = pack_load.load_nodes("pack-1", f, builder, id_map)
        assert (ok, skip, err) == (1, 0, 0), f"최초 적재 실패: {(ok, skip, err)}"
        assert vec0.rows() == {"n1"}, "최초 적재에서 벡터가 안 만들어졌다(전제 깨짐)"

        state = pack_load.live_pack_state("pack-1", graph, docs, vec0)

        # 벡터 슬롯만 유실 재현(부분 복원·백엔드 삭제) — graph/doc 은 그대로.
        vec1 = _EnumerableVecWithLookup("pack-1")
        builder._vec = vec1
        n_new, n_chg, n_same, skip2, err2, ids, vu = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state["nodes"], graph, docs,
            state["doc_node_spaces"], vec=vec1)
        assert (n_new, n_chg, n_same, skip2, err2) == (0, 1, 0, 0, 0), (
            f"벡터 유실이 same 으로 방치됐다: new={n_new} chg={n_chg} same={n_same}")
        assert vec1.rows() == {"n1"}, "벡터가 회수되지 않았다"
        assert vu == 0, "열거 가능 백엔드는 미확인이 아니라 즉시 판정돼야 한다"
        assert ids == {"n1"}

        # 2회차: 이제 벡터가 있으니 same 으로 수렴한다.
        state2 = pack_load.live_pack_state("pack-1", graph, docs, vec1)
        n_new2, n_chg2, n_same2, skip3, err3, _ids2, vu2 = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state2["nodes"], graph, docs,
            state2["doc_node_spaces"], vec=vec1)
        assert (n_new2, n_chg2, n_same2, skip3, err3, vu2) == (0, 0, 1, 0, 0, 0), (
            f"2회차가 same 으로 수렴하지 않았다: "
            f"{(n_new2, n_chg2, n_same2, skip3, err3, vu2)}")

    def test_removing_the_check_would_leave_the_vector_loss_forever(
            self, live, tmp_path, monkeypatch):
        """변형(검사 제거) red 확인 — `_live_vec_ids` 를 항상 None 으로
        되접는 스텁으로 몽키패치해 "검사 삭제" 상태를 흉내낸다. 그러면 벡터
        유실이 same 으로 영구 방치돼야 한다(위 회수 테스트가 실제로 이
        경로에 의존한다는 증거)."""
        builder, graph, docs = live
        vec0 = _EnumerableVecWithLookup("pack-1")
        builder._vec = vec0
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", f, builder, id_map)
        state = pack_load.live_pack_state("pack-1", graph, docs, vec0)

        vec1 = _EnumerableVecWithLookup("pack-1")  # 벡터 유실 재현(빈 스토어)
        builder._vec = vec1
        monkeypatch.setattr(pack_load, "_live_vec_ids", lambda vec, pack: None)
        _n, n_chg, n_same, _s, _e, _ids, _vu = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state["nodes"], graph, docs,
            state["doc_node_spaces"], vec=vec1)
        assert (n_chg, n_same) == (0, 1), (
            "검사가 무력화된 변형에서도 same 이 아니면 이 테스트가 회귀를 못 잡는다")
        assert vec1.rows() == set(), "변형에서는 벡터가 회수되면 안 된다(대조군)"


# ───────────────── 게이트 ㉳b — R1, 실 sqlite-vec 스토어(더블 아님) ─────────────
#
# 위 ㉳는 `_EnumerableVecWithLookup`(더블)로 R1 분기를 확인한다. 리드 지적
# (#377 구현 보고 검토): 라이브 서비스의 벡터 백엔드는 실제로
# `VECTOR_BACKEND=sqlite-vec`다 — 더블만으로는 실 백엔드의 열거 가능성
# (`_vec_shape`가 `_conn`/`_table`을 인식하고, `pack_id`가 실제 컬럼인 것)을
# 증명하지 못한다. 이 클래스는 `tests/_vec_helpers.build_vector_store`로 만든
# 진짜 `SqliteVecStore`를 그대로 태운다(`tests/test_pack_load.py`의
# `TestSlotOwnershipThroughTheRealStores.real_vec` 픽스처와 같은 헬퍼).


class TestNodeVectorOnlyLossRecoveryRealSqliteVec:
    """R1 회수가 더블이 아니라 실제 SqliteVecStore에서도 성립함을 보인다."""

    def test_vector_only_loss_is_recovered_against_real_sqlite_vec(
            self, live, tmp_path):
        builder, graph, docs = live
        vec = build_vector_store("sqlite-vec", tmp_path)
        assert vec.available, "실 SqliteVecStore가 available=False — 전제 깨짐"
        builder._vec = vec
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        id_map: dict = {}
        ok, skip, err = pack_load.load_nodes("pack-1", f, builder, id_map)
        assert (ok, skip, err) == (1, 0, 0), f"최초 적재 실패: {(ok, skip, err)}"
        assert vec.get_by_id("n1") is not None, (
            "최초 적재에서 실 스토어에 벡터가 안 만들어졌다(전제 깨짐)")

        state = pack_load.live_pack_state("pack-1", graph, docs, vec)

        # 벡터 슬롯만 유실 재현 — 공개 API(delete)로 이 팩의 벡터 행만 지운다.
        # graph/doc은 그대로다.
        vec.delete(["n1"])
        assert vec.get_by_id("n1") is None, "유실 재현이 실제로 행을 못 지웠다"

        n_new, n_chg, n_same, skip2, err2, ids, vu = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state["nodes"], graph, docs,
            state["doc_node_spaces"], vec=vec)
        assert (n_new, n_chg, n_same, skip2, err2) == (0, 1, 0, 0, 0), (
            f"실 sqlite-vec에서 벡터 유실이 same 으로 방치됐다: "
            f"new={n_new} chg={n_chg} same={n_same}")
        assert vec.get_by_id("n1") is not None, "실 스토어에서 벡터가 회수되지 않았다"
        assert vu == 0, "열거 가능한 실 백엔드는 미확인이 아니라 즉시 판정돼야 한다"
        assert ids == {"n1"}

        # 2회차: 이제 벡터가 있으니 same 으로 수렴한다.
        state2 = pack_load.live_pack_state("pack-1", graph, docs, vec)
        n_new2, n_chg2, n_same2, skip3, err3, _ids2, vu2 = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state2["nodes"], graph, docs,
            state2["doc_node_spaces"], vec=vec)
        assert (n_new2, n_chg2, n_same2, skip3, err3, vu2) == (0, 0, 1, 0, 0, 0), (
            f"2회차가 same 으로 수렴하지 않았다: "
            f"{(n_new2, n_chg2, n_same2, skip3, err3, vu2)}")

        if hasattr(vec, "close"):
            vec.close()


# ───────────────────────── 게이트 ㉴ — opt-in(열거 불가) ─────────────────────

class TestNodeOptInRecoversViaSingleLookup:
    """열거 불가 백엔드 + `recover_vectors=True` — 단건 조회로 회수한다."""

    def test_opt_in_recovers_the_lost_vector_then_converges_to_same(
            self, live, tmp_path):
        builder, graph, docs = live
        vec = _UnenumerableVecWithLookup()
        builder._vec = vec
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        id_map: dict = {}
        ok, skip, err = pack_load.load_nodes("pack-1", f, builder, id_map)
        assert (ok, skip, err) == (1, 0, 0), f"최초 적재 실패: {(ok, skip, err)}"
        assert "n1" in vec.rows, "최초 적재에서 벡터가 안 만들어졌다(전제 깨짐)"

        state = pack_load.live_pack_state("pack-1", graph, docs, vec)
        del vec.rows["n1"]  # 벡터만 유실(부분 삭제) 재현 — graph/doc 은 그대로
        vec.calls.clear()
        vec.get_by_id_calls.clear()

        n_new, n_chg, n_same, skip2, err2, ids, vu = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state["nodes"], graph, docs,
            state["doc_node_spaces"], vec=vec, recover_vectors=True)
        assert (n_new, n_chg, n_same, skip2, err2) == (0, 1, 0, 0, 0), (
            f"단건 조회 회수가 chg 경로로 재기록시키지 않았다: "
            f"{(n_new, n_chg, n_same, skip2, err2)}")
        assert vu == 0, "회수됐으면 미확인 건수는 0 이어야 한다"
        assert "n1" in vec.rows, "벡터가 실제로 회수되지 않았다"

        # 2회차: 이제 벡터가 있으니 same 으로 수렴한다.
        state2 = pack_load.live_pack_state("pack-1", graph, docs, vec)
        n_new2, n_chg2, n_same2, skip3, err3, _ids2, vu2 = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state2["nodes"], graph, docs,
            state2["doc_node_spaces"], vec=vec, recover_vectors=True)
        assert (n_new2, n_chg2, n_same2, skip3, err3, vu2) == (0, 0, 1, 0, 0, 0), (
            f"2회차가 same 으로 수렴하지 않았다: "
            f"{(n_new2, n_chg2, n_same2, skip3, err3, vu2)}")


# ─────────────── 게이트 ㉵a — 벡터 축 부재(vec=None)는 유실이 아니다 ───────────
#
# #377 v3(구현 보고 검토 뒤 리드 지시, 설계 v3 §4): `vec is None`은 이
# 배포에 벡터 축 자체가 없다는 뜻이지 유실이 아니다. 아래 opt-out 갈래
# (㉵b, `vec`이 있지만 열거 불가+조회 수단 없음)와 달리 `vec_unrecovered`를
# 올리지 않고 원인별 경고도 내지 않는다.


class TestNodeVectorAxisAbsentIsNotLoss:
    """`vec=None` — 유실이 아니라 부재. 카운터도 경고도 없다(대조군은 ㉵b)."""

    def test_vec_none_yields_zero_unrecovered_and_no_warning(
            self, live, tmp_path, caplog):
        builder, graph, docs = live
        # 최초 적재 자체는 벡터 스토어가 있는 상태로 해야 same 판정까지
        # 도달한다(벡터 없이 최초 적재하면 graph/doc 자체가 live 아님).
        vec0 = _UnenumerableVecWithLookup()
        builder._vec = vec0
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        id_map: dict = {}
        ok, skip, err = pack_load.load_nodes("pack-1", f, builder, id_map)
        assert (ok, skip, err) == (1, 0, 0)
        state = pack_load.live_pack_state("pack-1", graph, docs, vec0)
        # 위 live_pack_state(vec0) 호출 자체가 "열거 미지원 백엔드" 경고를
        # 낼 수 있다(vec0 은 비열거 더블) — 이 테스트가 보려는 것은
        # load_nodes_incremental(vec=None) 호출 하나에서 나는 경고이므로,
        # 그 호출 직전에 캡처를 비운다.
        caplog.clear()

        with caplog.at_level(logging.DEBUG, logger="opencrab.pack.load"):
            n_new, n_chg, n_same, skip2, err2, _ids, vu = pack_load.load_nodes_incremental(
                "pack-1", f, builder, id_map, state["nodes"], graph, docs,
                state["doc_node_spaces"])  # vec 생략(None) — 벡터 축 없는 배포
        assert (n_new, n_chg, n_same, skip2, err2) == (0, 0, 1, 0, 0), (
            "vec=None 인 배포도 분류(same/chg)는 종전과 같이 보존돼야 한다: "
            f"{(n_new, n_chg, n_same, skip2, err2)}")
        assert vu == 0, (
            "vec=None 은 유실이 아니라 부재다(설계 v3 §4) — vec_unrecovered 를 "
            f"올리면 안 된다: 실측 {vu}")
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], (
            f"vec=None 배포에서 경고가 나면 안 된다(매 런 반복 잡음 금지): "
            f"{[r.getMessage() for r in warnings]}")

    def test_vec_none_logs_at_most_one_debug_line_per_run(
            self, live, tmp_path, caplog):
        builder, graph, docs = live
        vec0 = _UnenumerableVecWithLookup()
        builder._vec = vec0
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", f, builder, id_map)
        state = pack_load.live_pack_state("pack-1", graph, docs, vec0)
        caplog.clear()

        with caplog.at_level(logging.DEBUG, logger="opencrab.pack.load"):
            pack_load.load_nodes_incremental(
                "pack-1", f, builder, id_map, state["nodes"], graph, docs,
                state["doc_node_spaces"])  # vec 생략(None)
        axis_absent_lines = [
            r for r in caplog.records if "벡터 축 없음" in r.getMessage()]
        assert len(axis_absent_lines) == 1, (
            f"런당 정확히 1회여야 한다: {[r.getMessage() for r in caplog.records]}")
        assert axis_absent_lines[0].levelno == logging.DEBUG, (
            "부재는 반복 경고가 아니라 단발 관측 로그다(WARNING 이 아니어야 한다)")


# ────────────── 게이트 ㉵b — opt-out 기본값(대조군: vec 은 있다) ───────────────


class TestNodeOptOutDefaultPreservesBehaviorButFlagsIt:
    """열거 불가 + opt-out(기본값), `vec`은 있다 — 회수는 안 하지만 침묵하지
    않는다. ㉵a(vec=None)의 대조군: `vec`이 있으면 여전히 경고가 난다."""

    def test_default_leaves_it_unrecovered_but_counted_and_logged(
            self, live, tmp_path, caplog):
        builder, graph, docs = live
        vec = _UnenumerableVecWithLookup()
        builder._vec = vec
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        id_map: dict = {}
        ok, skip, err = pack_load.load_nodes("pack-1", f, builder, id_map)
        assert (ok, skip, err) == (1, 0, 0)

        state = pack_load.live_pack_state("pack-1", graph, docs, vec)
        del vec.rows["n1"]  # 벡터만 유실 재현
        vec.get_by_id_calls.clear()

        with caplog.at_level(logging.WARNING, logger="opencrab.pack.load"):
            n_new3, n_chg3, n_same3, skip3, err3, _ids3, vu3 = pack_load.load_nodes_incremental(
                "pack-1", f, builder, id_map, state["nodes"], graph, docs,
                state["doc_node_spaces"], vec=vec)  # recover_vectors 기본값(False)
        assert (n_new3, n_chg3, n_same3, skip3, err3) == (0, 0, 1, 0, 0), (
            "recover_vectors 기본값(False)에서 카운트 산출이 달라졌다: "
            f"{(n_new3, n_chg3, n_same3, skip3, err3)}")
        # 대조군: vec=None(㉵a)과 달리 vec 은 실제로 있다 — 열거 불가 +
        # opt-out 이므로 여전히 미확인으로 드러나야 한다(설계 v3 §4, "vec이
        # None 이 아니면서" 조건으로 ㉵a 와 분리됨).
        assert vu3 == 1, (
            "vec 이 있는데 열거 불가+opt-out 이면 여전히 미확인 건수로 드러나야 "
            "한다(vec=None 갈래와 혼동하면 안 된다)")
        assert vec.get_by_id_calls == [], "opt-out 인데 단건 조회를 시도했다"

    def test_opt_out_summary_warning_has_the_right_branch_text(
            self, live, tmp_path, caplog):
        builder, graph, docs = live
        vec = _UnenumerableVecWithLookup()
        builder._vec = vec
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        id_map: dict = {}
        pack_load.load_nodes("pack-1", f, builder, id_map)
        state = pack_load.live_pack_state("pack-1", graph, docs, vec)
        del vec.rows["n1"]

        with caplog.at_level(logging.WARNING, logger="opencrab.pack.load"):
            pack_load.load_nodes_incremental(
                "pack-1", f, builder, id_map, state["nodes"], graph, docs,
                state["doc_node_spaces"], vec=vec)
        summary = _summary_records(caplog)
        assert len(summary) == 1, (
            f"opt-out 요약 로그는 정확히 1건이어야 한다: "
            f"{[r.getMessage() for r in caplog.records]}")
        msg = summary[0].getMessage()
        assert "recover_vectors=True 로 단건 조회 회수를 켤 수 있다" in msg, msg


# ─────────── 게이트 ㉳c: 인가 경계, 벡터 접근은 principal 게이트 뒤 ───────────
#
# PR #421 인라인 리뷰(codex, P2): #377이 새로 넣은 R1 벡터 열거
# (`_live_vec_ids`)가 `_require_bound_principal()`보다 먼저 실행됐다.
# 미인증 호출이 principal 오류 전에 벡터 백엔드에 먼저 닿을 수 있었다.
# 형제 축 `load_chunks_incremental`(require_live_data, principal,
# authorize, 벡터 열거 순)과 순서를 맞춰 고친 뒤, 그 순서를 행동으로 거는
# 게이트다. `tests/test_pack_load_chunk_authz.py::TestUnboundPrincipal`
# (#205)과 같은 미바인딩 패턴이되, 거기 없던 벡터 열거 호출 횟수 스파이를
# 더한다. #332가 청크 축에 `_live_vec_ids`를 넣었을 때 그 시험 파일은
# 갱신되지 않아 이 종류의 순서 역전을 못 잡는다(청크 축 현재 순서는
# 이미 올발라 지금 당장의 결함은 아니다. #377/#421 범위 밖, 비차단
# 발견으로 남긴다).


class TestNodeVecAccessGatedByPrincipal:
    """`load_nodes_incremental`의 벡터 접근(R1 열거 + opt-in 단건 조회)은
    `_require_bound_principal()`을 통과한 뒤에만 실행돼야 한다."""

    @staticmethod
    def _spy_live_vec_ids(monkeypatch):
        """`pack_load._live_vec_ids` 호출 횟수만 세고 실제 함수에 위임한다.

        `_EnumerableVecWithLookup`(kind=sql 인식 더블)을 그대로 태우기
        위해 원 함수 로직은 바꾸지 않는다. 호출 여부와 횟수만 관찰 대상."""
        calls = {"n": 0}
        real = pack_load._live_vec_ids

        def _wrapped(vec, pack_name):
            calls["n"] += 1
            return real(vec, pack_name)

        monkeypatch.setattr(pack_load, "_live_vec_ids", _wrapped)
        return calls

    def test_unbound_principal_blocks_vector_access(self, tmp_path, monkeypatch, pack_sql):
        """RED(수정 전): principal을 전혀 바인딩하지 않고 호출한다. 인가
        오류 자체는 수정 전에도 나지만(§ 최종적으로 `_require_bound_principal`
        은 결국 불린다), 그 전에 벡터 열거와 단건 조회가 이미 실행돼 버렸는지를
        스파이 카운트로 잡는다. 이 카운트 단언이 재배치 전에는 실패한다."""
        monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
        graph = LocalGraphStore(str(tmp_path / "graph.db"))
        docs = LocalSQLDocStore(str(tmp_path / "doc.db"))
        builder = OntologyBuilder(graph, docs, pack_sql)

        vec0 = _EnumerableVecWithLookup("pack-1")
        builder._vec = vec0
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        id_map: dict = {}
        principal = Principal(user_id=_LIVE_TEST_USER, is_local=True, disabled=False)
        with principal_scope(principal):
            ok, skip, err = pack_load.load_nodes("pack-1", f, builder, id_map)
        assert (ok, skip, err) == (1, 0, 0), "베이스라인 적재 자체가 실패했다. 전제가 깨졌다"
        state = pack_load.live_pack_state("pack-1", graph, docs, vec0)

        vec1 = _EnumerableVecWithLookup("pack-1")
        builder._vec = vec1
        enum_calls = self._spy_live_vec_ids(monkeypatch)
        lookup_calls = {"n": 0}
        real_get_by_id = vec1.get_by_id

        def _spy_get_by_id(doc_id):
            lookup_calls["n"] += 1
            return real_get_by_id(doc_id)

        monkeypatch.setattr(vec1, "get_by_id", _spy_get_by_id)

        # principal_scope 를 전혀 열지 않는다. 로더가 스스로 principal 을
        # 바인딩하지 않는다는 #148 의도를 그대로 따른다.
        try:
            with pytest.raises(RuntimeError, match="principal_scope"):
                pack_load.load_nodes_incremental(
                    "pack-1", f, builder, id_map, state["nodes"], graph, docs,
                    state["doc_node_spaces"], vec=vec1)

            assert enum_calls["n"] == 0, (
                "미바인딩 principal 인데 벡터 열거(_live_vec_ids)가 실행됐다. "
                "인가 경계보다 먼저 벡터 백엔드에 닿았다")
            assert lookup_calls["n"] == 0, (
                "미바인딩 principal 인데 단건 조회(get_by_id)가 실행됐다")
        finally:
            graph.close()
            docs.close()

    def test_bound_principal_control_group_recovery_still_fires(
            self, live, tmp_path, monkeypatch):
        """대조군: principal 이 있으면 재배치 뒤에도 R1 회수가 그대로
        동작하고, 열거는 실행당 정확히 1회다."""
        builder, graph, docs = live
        vec0 = _EnumerableVecWithLookup("pack-1")
        builder._vec = vec0
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        id_map: dict = {}
        ok, skip, err = pack_load.load_nodes("pack-1", f, builder, id_map)
        assert (ok, skip, err) == (1, 0, 0)
        state = pack_load.live_pack_state("pack-1", graph, docs, vec0)

        vec1 = _EnumerableVecWithLookup("pack-1")
        builder._vec = vec1
        enum_calls = self._spy_live_vec_ids(monkeypatch)

        n_new, n_chg, n_same, skip2, err2, ids, vu = pack_load.load_nodes_incremental(
            "pack-1", f, builder, id_map, state["nodes"], graph, docs,
            state["doc_node_spaces"], vec=vec1)

        assert enum_calls["n"] == 1, "정상 경로에서 열거가 실행당 1회가 아니다"
        assert (n_new, n_chg, n_same, skip2, err2, vu) == (0, 1, 0, 0, 0, 0), (
            "principal 이 있는 정상 호출에서 R1 회수가 깨졌다")
        assert vec1.rows() == {"n1"}, "벡터가 회수되지 않았다"
        assert ids == {"n1"}
