"""r12 — #142 재리뷰(26abfce 기준) 신규 3건: 증분 자가치유 결손 폐쇄 게이트.

`design_fix_round12.md`(적대 검수 PASS, 첫 라운드)의 폐쇄 게이트 ㉮~㉲ 중
이 파일이 담당하는 부분을 코드로 건다. 전부 "유실 상태가 same/미계수 판정에
가려 영구 미회수" 클래스다.

- R1 (P1): `load_chunks_incremental` 의 c_same 경로가 벡터만 유실된 상태를
  방치한다 — 텍스트·메타가 라이브와 같으면 벡터 존재를 안 보고 same 으로
  끝난다.
- R2 (P2): `load_nodes_incremental` 의 n_same 경로가 현재 space 의 doc 행
  유실을 방치한다 — graph 는 same 인데 doc 만 없는 상태(지난 런의 부분
  실패 잔재)가 영구화된다.
- R3 (P2): 구 타입 행 삭제가 실패하면 warning 만 남기고 err 미계수, 그리고
  `live_pack_state` 가 node_id 로 collapse 해 다음 런이 same 으로 보고
  재시도하지 않는다 — 구 행 + cascade 엣지가 영구 잔존한다.

기존 픽스처·더블은 `tests/test_pack_load.py`(진짜 SQLite 3스토어)와
`tests/test_pack_load_r11_pg_gates.py`(PG형 fake)에서 재사용한다(세 번째
사본 방지).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from opencrab.pack import load as pack_load
from tests.test_pack_load import (  # noqa: F401 — 기존 픽스처·더블 재사용
    _NoVec,
    _RecordingVec,
    _node,
    _write_jsonl,
    live,
)
from tests.test_pack_load_r11_pg_gates import _pg_fakes


# ───────────────────────── R1 전용 더블 ─────────────────────────

class _EnumerableVec:
    """`_vec_backend()` 가 `kind="sql"` 로 인식하는 형태(`_conn`+`_table`+
    `pack_id` 컬럼)이면서 `upsert_texts` 도 지원한다.

    기존 `_RecordingVec` 은 전자가 없다(`_conn`/`_table`/`_collection`/
    `_engine` 어느 것도 없어 kind=None — R1 검사가 항상 skip 된다, 회귀
    안전판으로는 맞지만 R1 회수 경로 자체는 이 더블로 못 켠다). 기존
    `_SqliteVecLike` 는 후자가 없다(`upsert_texts` 미구현이라
    `load_chunks_incremental` 의 flush 를 못 받는다). 둘을 합쳐야 "벡터
    존재 검사 → 재임베딩 회수" 전체가 실제로 지나간다.
    """

    _table = "vectors_kure"

    def __init__(self, pack_id: str = "pack-1"):
        self.available = True
        self.pack_id = pack_id
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            f"CREATE TABLE {self._table} "
            "(node_id TEXT PRIMARY KEY, pack_id TEXT, metadata TEXT)")
        self._conn.commit()
        self.upsert_calls: list[list[str]] = []

    def rows(self) -> set[str]:
        return {r[0] for r in self._conn.execute(f"SELECT node_id FROM {self._table}")}

    def upsert_texts(self, texts, ids=None, metadatas=None):
        ids = list(ids or [])
        self.upsert_calls.append(ids)
        for i in ids:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {self._table}(node_id, pack_id) VALUES (?, ?)",
                (i, self.pack_id))
        self._conn.commit()

    def delete(self, ids):
        ids = list(ids)
        if not ids:
            return
        ph = ",".join("?" * len(ids))
        self._conn.execute(f"DELETE FROM {self._table} WHERE node_id IN ({ph})", ids)
        self._conn.commit()


def _chunk_row(cid: str, text: str = "본문", **meta) -> dict:
    return {"id": cid, "document_id": "n1", "text": text, "metadata": meta}


def _live_chunks_from_docs(docs) -> dict[str, tuple[str, dict]]:
    return {sid: (txt, json.loads(md)) for sid, txt, md in docs._conn.execute(
        "SELECT source_id, text, metadata FROM doc_sources")}


# ───────────────────────── 게이트 ㉮ — R1 ─────────────────────────

class TestLiveVecIdsHelperContract:
    """`_live_vec_ids` 자체의 계약 — kind 판별 기준(review 권고 #1)."""

    def test_unavailable_backend_returns_none(self):
        assert pack_load._live_vec_ids(_NoVec(), "pack-1") is None

    def test_available_but_unrecognized_backend_returns_none(self):
        """`available=True` 이지만 `_conn`/`_collection`/`_engine` 어느 것도
        없는 백엔드(`_RecordingVec`) — kind 는 None 이다. `vec.available` 만
        보면 이 경우를 "가용"으로 오판해 빈 집합을 돌려주게 된다."""
        assert pack_load._live_vec_ids(_RecordingVec(), "pack-1") is None

    def test_recognized_sql_backend_returns_a_set(self):
        vec = _EnumerableVec("pack-1")
        assert pack_load._live_vec_ids(vec, "pack-1") == set()
        vec.upsert_texts(texts=["t"], ids=["c1"], metadatas=[{}])
        assert pack_load._live_vec_ids(vec, "pack-1") == {"c1"}


class TestVectorOnlyLossRecovery:
    """R1 — c_same 경로가 벡터만 유실된 상태를 회수해야 한다."""

    def test_vector_only_loss_is_recovered_as_txt_then_converges_to_same(
            self, live, tmp_path):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        vec0 = _EnumerableVec("pack-1")
        pack_load.load_chunks("pack-1", f, vec0, docs)
        live_chunks = _live_chunks_from_docs(docs)

        # 벡터축 전체 유실 재현(부분 복원·백엔드 삭제) — doc 은 그대로.
        vec1 = _EnumerableVec("pack-1")
        c_new, c_txt, c_meta, c_same, err, ids = pack_load.load_chunks_incremental(
            "pack-1", f, vec1, docs, live_chunks)
        assert (c_new, c_txt, c_meta, c_same, err) == (0, 1, 0, 0, 0), (
            f"벡터 유실이 same 으로 방치됐다: new={c_new} txt={c_txt} meta={c_meta} same={c_same}")
        assert vec1.rows() == {"c1"}, "벡터가 회수되지 않았다"
        assert ids == {"c1"}

        # 2회차: 이제 벡터가 있으니 same 으로 수렴한다.
        live_chunks2 = _live_chunks_from_docs(docs)
        c_new2, c_txt2, c_meta2, c_same2, err2, _ids2 = pack_load.load_chunks_incremental(
            "pack-1", f, vec1, docs, live_chunks2)
        assert (c_new2, c_txt2, c_meta2, c_same2, err2) == (0, 0, 0, 1, 0), (
            f"2회차가 same 으로 수렴하지 않았다: {(c_new2, c_txt2, c_meta2, c_same2, err2)}")
        assert vec1.upsert_calls == [["c1"]], "2회차에 재임베딩이 또 일어났다"

    def test_vec_unavailable_skips_the_check_and_keeps_current_behavior(
            self, live, tmp_path):
        """벡터 축 없는 배포(kind None) — 검사 자체가 skip 되고 현행 동작
        (텍스트·메타 동일이면 same, 재임베딩 없음)이 보존돼야 한다."""
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        pack_load.load_chunks("pack-1", f, _RecordingVec(), docs)
        live_chunks = _live_chunks_from_docs(docs)

        vec2 = _RecordingVec()
        c_new, c_txt, c_meta, c_same, err, _ids = pack_load.load_chunks_incremental(
            "pack-1", f, vec2, docs, live_chunks)
        assert (c_new, c_txt, c_meta, c_same, err) == (0, 0, 0, 1, 0)
        assert vec2.calls == [], "vec 미가용인데 재임베딩을 호출했다"

    def test_meta_changed_with_vector_absent_still_self_heals_via_txt(
            self, live, tmp_path):
        """기존 c_meta 자가치유(부재→`_vec_meta_update` False→txt 우회) 회귀
        확인 — R1 이 메타 분기보다 뒤에 있으므로 이 경로엔 관여하지 않아야
        한다."""
        _b, _g, docs = live
        f1 = _write_jsonl(tmp_path / "c1.jsonl", [_chunk_row("c1", "본문", 쪽="3")])
        pack_load.load_chunks("pack-1", f1, _RecordingVec(), docs)
        live_chunks = _live_chunks_from_docs(docs)

        f2 = _write_jsonl(tmp_path / "c2.jsonl", [_chunk_row("c1", "본문", 쪽="99")])
        vec = _EnumerableVec("pack-1")  # 인식되는 백엔드이지만 벡터는 부재
        c_new, c_txt, c_meta, c_same, err, _ids = pack_load.load_chunks_incremental(
            "pack-1", f2, vec, docs, live_chunks)
        assert (c_txt, c_meta, c_same) == (1, 0, 0), (
            f"벡터 부재에서 메타 경로가 txt 로 안 우회했다(기존 자가치유 회귀): "
            f"txt={c_txt} meta={c_meta} same={c_same}")

    def test_removing_the_check_would_leave_the_vector_loss_forever(
            self, live, tmp_path, monkeypatch):
        """변형(검사 제거) red 확인 — `_live_vec_ids` 를 항상 None 으로
        되접는 스텁으로 몽키패치해 "검사 삭제" 상태를 흉내낸다. 그러면 벡터
        유실이 same 으로 영구 방치돼야 한다(위 회수 테스트가 실제로 이
        경로에 의존한다는 증거)."""
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        vec0 = _EnumerableVec("pack-1")
        pack_load.load_chunks("pack-1", f, vec0, docs)
        live_chunks = _live_chunks_from_docs(docs)

        vec1 = _EnumerableVec("pack-1")  # 벡터 유실 재현(빈 스토어)
        monkeypatch.setattr(pack_load, "_live_vec_ids", lambda vec, pack: None)
        _n, c_txt, _m, c_same, _e, _ids = pack_load.load_chunks_incremental(
            "pack-1", f, vec1, docs, live_chunks)
        assert (c_txt, c_same) == (0, 1), (
            "검사가 무력화된 변형에서도 same 이 아니면 이 테스트가 회귀를 못 잡는다")
        assert vec1.rows() == set(), "변형에서는 벡터가 회수되면 안 된다(대조군)"


# ───────────────────────── 게이트 ㉯ — R2 ─────────────────────────

class TestDocRowLossRecovery:
    """R2 — n_same 경로가 현재 space 의 doc 행 유실을 회수해야 한다."""

    def test_doc_row_only_loss_is_recovered_as_chg_then_converges_to_same(
            self, live, tmp_path):
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, builder, {})
        assert docs._conn.execute(
            "SELECT COUNT(*) FROM doc_nodes WHERE space=? AND node_id=?",
            ("resource", "n1")).fetchone()[0] == 1, "사전조건: doc 행이 있어야 한다"

        # doc 행만 유실 — graph 는 그대로(지난 런의 add_node 가 graph 는 쓰고
        # doc 만 실패한 잔재를 재현).
        docs._conn.execute(
            "DELETE FROM doc_nodes WHERE space=? AND node_id=?", ("resource", "n1"))
        docs._conn.commit()

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert "n1" not in state["doc_node_spaces"], "전제: doc 행이 사라져야 한다"

        n_new, n_chg, n_same, skip, err, _ids = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, state["nodes"], graph, docs, state["doc_node_spaces"])
        assert (n_new, n_chg, n_same, skip, err) == (0, 1, 0, 0, 0), (
            f"doc 행 유실이 same 으로 방치됐다: new={n_new} chg={n_chg} same={n_same}")
        assert docs._conn.execute(
            "SELECT COUNT(*) FROM doc_nodes WHERE space=? AND node_id=?",
            ("resource", "n1")).fetchone()[0] == 1, "doc 행이 회수되지 않았다"

        # 2회차: doc 행이 이제 존재하니 same 으로 수렴한다.
        state2 = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        n_new2, n_chg2, n_same2, skip2, err2, _ids2 = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, state2["nodes"], graph, docs, state2["doc_node_spaces"])
        assert (n_new2, n_chg2, n_same2, skip2, err2) == (0, 0, 1, 0, 0), (
            f"2회차가 same 으로 수렴하지 않았다: {(n_new2, n_chg2, n_same2, skip2, err2)}")

    def test_anchor_node_is_not_reloaded_when_doc_node_spaces_lacks_it(
            self, live, tmp_path):
        """F4-b 는 앵커를 `doc_node_spaces` 에서 아예 뺀다 — R2 검사가 앵커를
        예외하지 않으면 앵커마다 매 런 "doc 행 없음"으로 오판해 재적재
        루프가 열린다(설계 권고 #2)."""
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl",
                          [_node(id="dataset:foo", node_type="Dataset", space="resource")])
        pack_load.load_nodes("pack-1", f, builder, {})

        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert "dataset:foo" not in state["doc_node_spaces"], (
            "전제: F4-b 가 앵커를 doc_node_spaces 에서 뺀다")

        n_new, n_chg, n_same, skip, err, _ids = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, state["nodes"], graph, docs, state["doc_node_spaces"])
        assert (n_new, n_chg, n_same, skip, err) == (0, 0, 1, 0, 0), (
            f"앵커 노드가 doc_node_spaces 부재로 오탐 재적재됐다: "
            f"new={n_new} chg={n_chg} same={n_same}")

    def test_removing_the_check_would_leave_the_doc_row_loss_forever(
            self, live, tmp_path, monkeypatch):
        """변형(검사 제거) red 확인 — `doc_node_spaces` 를 항상 실측대로
        보이게 강제하는 대신, 검사 조건 자체를 무력화(F4-c 정리 함수처럼
        "항상 존재"로 보이게)하면 doc 행 유실이 same 으로 영구 방치돼야
        한다."""
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, builder, {})
        docs._conn.execute(
            "DELETE FROM doc_nodes WHERE space=? AND node_id=?", ("resource", "n1"))
        docs._conn.commit()
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())

        orig = pack_load._is_anchor_node
        # R2 검사를 무력화하는 가장 직접적인 방법: 모든 노드를 "앵커"로
        # 보이게 만들어 `doc_row_missing` 판정이 항상 False 로 꺾이게 한다
        # (실코드 조건 `not _is_anchor_node(...) and ...` 의 첫 항을 죽인다).
        monkeypatch.setattr(pack_load, "_is_anchor_node", lambda *a, **kw: True)
        n_new, n_chg, n_same, skip, err, _ids = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, state["nodes"], graph, docs, state["doc_node_spaces"])
        monkeypatch.setattr(pack_load, "_is_anchor_node", orig)

        assert (n_chg, n_same) == (0, 1), (
            "검사가 무력화된 변형에서도 same 이 아니면 이 테스트가 회귀를 못 잡는다")
        assert docs._conn.execute(
            "SELECT COUNT(*) FROM doc_nodes WHERE space=? AND node_id=?",
            ("resource", "n1")).fetchone()[0] == 0, "변형에서는 doc 행이 회수되면 안 된다(대조군)"


# ───────────────────────── 게이트 ㉰ — R3 ─────────────────────────

class TestDupTypeNodeRowsHelper:
    """`_dup_type_node_rows` 단위 계약 — PG형 fake 에서 방언 위반 없이
    동작해야 한다(게이트 ㉱)."""

    def test_groups_by_node_id_within_the_named_pack_only(self):
        graph, _docs = _pg_fakes()
        graph.seed_node("Document", "n1", "pack-1")
        graph.seed_node("Concept", "n1", "pack-1")     # 중복 타입 인위 주입
        graph.seed_node("Document", "n2", "pack-1")    # 중복 없음
        # 다른 팩 — 안 섞여야 함(PK 가 (node_type,node_id) 전역이라 같은
        # node_id 를 쓰려면 타입을 달리해야 이 fake 의 UNIQUE 제약과 안 부딪힌다).
        graph.seed_node("Agent", "n1", "pack-2")

        got = pack_load._dup_type_node_rows(graph, "pack-1")
        assert got == {"n1": {"Document", "Concept"}, "n2": {"Document"}}

    def test_sqlite_backend_agrees(self, live, tmp_path):
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1", node_type="Document")])
        pack_load.load_nodes("pack-1", f, builder, {})
        got = pack_load._dup_type_node_rows(graph, "pack-1")
        assert got == {"n1": {"Document"}}


class TestStaleTypeRowSweep:
    """R3 — 구 타입 행 삭제 실패의 즉시 신호(err+1) + 구조적 회수(매 런 스윕)."""

    def _seed_duplicate_type_row(self, live_fixture, tmp_path, monkeypatch):
        """1회차: `graph.delete_node` 를 주입 실패시켜 구 타입 행이 물리적으로
        남게 만든다(신 타입 행은 정상 저장). **스윕도 이 1회차 동안은
        무력화한다**(`_dup_type_node_rows`→`{}`) — 안 그러면 같은 런 말미의
        스윕이 방금 생긴 중복을 즉시 재시도해(같은 delete_node 가 아직
        깨져 있으므로 또 실패) err 이 "즉시 신호분"과 "스윕 재시도분"으로
        섞인다. 두 메커니즘을 독립적으로 검사하려고 분리한다 — `File` 은
        `Document` 와 같은 `resource` space 에서 유효한 타입이다(`Concept`
        같은 다른 space 전용 타입을 쓰면 그래프 스토어가 조용히
        `original_type` 으로 강등한다, 2026-08-13 실측).
        반환: (builder, graph, docs, f_new, err1)."""
        builder, graph, docs = live_fixture
        f_old = _write_jsonl(tmp_path / "old.jsonl",
                             [_node(id="n1", node_type="Document", space="resource")])
        pack_load.load_nodes("pack-1", f_old, builder, {})
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())

        monkeypatch.setattr(graph, "delete_node", lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("주입된 그래프 삭제 실패")))
        monkeypatch.setattr(pack_load, "_dup_type_node_rows", lambda *a, **kw: {})

        f_new = _write_jsonl(tmp_path / "new.jsonl",
                             [_node(id="n1", node_type="File", space="resource")])
        n_new, n_chg, n_same, skip, err1, _ids = pack_load.load_nodes_incremental(
            "pack-1", f_new, builder, {}, state["nodes"], graph, docs, state["doc_node_spaces"])

        assert n_chg == 1, f"타입 변경이 chg 로 안 세어졌다: {n_chg}"
        assert graph.get_node("Document", "n1") is not None, (
            "사전조건: 삭제가 실패했으니 구 행이 살아있어야 한다")
        assert graph.get_node("File", "n1") is not None, "신 행이 저장돼야 한다"

        monkeypatch.undo()  # delete_node·스윕 패치를 함께 되돌린다 — 이후 호출은 실동작
        return builder, graph, docs, f_new, err1

    def test_delete_failure_is_signaled_as_err_immediately(
            self, live, tmp_path, monkeypatch):
        _b, _g, _d, _f, err1 = self._seed_duplicate_type_row(live, tmp_path, monkeypatch)
        assert err1 == 1, f"삭제 실패가 err 로 즉시 안 잡혔다(즉시 신호, R3): err={err1}"

    def test_sweep_recovers_the_duplicate_on_the_next_run(
            self, live, tmp_path, monkeypatch):
        builder, graph, docs, f_new, _err1 = self._seed_duplicate_type_row(
            live, tmp_path, monkeypatch)

        # collapse 확인 — live_pack_state 는 node_id 로 collapse 해 신 타입만
        # 보인다(구 타입 행은 라이브 조회에서 안 보이지만 물리적으로는 있다).
        state2 = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert state2["nodes"]["n1"][0] == "File", "전제: collapse 가 신 타입을 보여준다"

        n_new2, n_chg2, n_same2, skip2, err2, _ids2 = pack_load.load_nodes_incremental(
            "pack-1", f_new, builder, {}, state2["nodes"], graph, docs, state2["doc_node_spaces"])

        assert n_same2 == 1, f"전제: collapse 뒤 같은 파일 재적재는 same 이어야 한다: {n_same2}"
        assert err2 == 0, f"복구된 뒤 스윕 삭제가 실패하면 안 된다: {err2}"
        assert graph.get_node("Document", "n1") is None, (
            "스윕이 구 타입 행을 회수하지 못했다 — collapse 뒤에 영구 잔존")
        assert graph.get_node("File", "n1") is not None, (
            "신 타입 행이 스윕에 휘말려 지워졌다")

    def test_removing_the_sweep_would_leave_the_duplicate_forever(
            self, live, tmp_path, monkeypatch):
        """변형(스윕 제거) red 확인 — `_dup_type_node_rows` 를 항상 빈 dict
        로 되접는 스텁으로 몽키패치해 "스윕 삭제" 상태를 흉내낸다. 그러면
        collapse 뒤 구 타입 행이 영구 잔존해야 한다(위 회수 테스트가 실제로
        스윕에 의존한다는 증거 — 즉시 err 신호만으로는 이 케이스를 못
        고친다)."""
        builder, graph, docs, f_new, _err1 = self._seed_duplicate_type_row(
            live, tmp_path, monkeypatch)
        state2 = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())

        monkeypatch.setattr(pack_load, "_dup_type_node_rows", lambda *a, **kw: {})
        pack_load.load_nodes_incremental(
            "pack-1", f_new, builder, {}, state2["nodes"], graph, docs, state2["doc_node_spaces"])

        assert graph.get_node("Document", "n1") is not None, (
            "스윕이 무력화된 변형에서도 구 행이 사라지면 이 테스트가 회귀를 못 잡는다")
