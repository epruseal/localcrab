"""r14 — #332: 열거 불가 벡터 백엔드에서 증분 적재가 벡터 유실을 회수 못 함.

`_live_vec_ids` 가 벡터 ID 를 **열거**할 수 없는 백엔드(`_vec_shape` 가
kind=None 으로 판정)에서는 종전에 벡터 존재 확인 자체를 skip 하고 텍스트·
메타가 라이브와 같은 청크를 바로 `c_same` 처리했다. 그 백엔드에서 벡터만
유실돼도 텍스트·메타는 계속 같으므로 매 증분이 다시 `c_same` 으로 떨어져
**영구히 회수되지 않는다**(형제 R1 게이트 — `test_pack_load_r12_selfheal_
gates.py::TestVectorOnlyLossRecovery` — 는 kind 가 인식되는 백엔드만 다뤄
이 틈을 덮지 않는다).

`design_v3.md`(codex 3라운드 AGREE)의 수정: `recover_vectors=True` 옵트인
+ 백엔드가 `get_by_id(doc_id) -> dict | None` 을 노출하면 단건 조회로 존재를
확인해 회수한다. 이 파일의 더블(`_UnenumerableVecWithLookup`)은 3스토어가
쓰는 [#197] 소유권 게이트 함수(`opencrab.stores._vector_base`)를 그대로
재사용한다 — 손으로 짠 검사는 예외 문구에 남의 팩 이름을 노출해
`reject_foreign_slot_writes` 의 "THE MESSAGE NEVER NAMES THE OTHER PACK"
불변식(localcrab#143 invariant 7)을 어긴다(design_v3 결함 5).

기존 픽스처·더블은 `tests/test_pack_load.py`(진짜 SQLite 3스토어)에서
재사용한다(세 번째 사본 방지).
"""
from __future__ import annotations

import json

from opencrab.pack import load as pack_load
from opencrab.stores._vector_base import (
    default_metadatas,
    reject_batch_pack_conflicts,
    reject_foreign_slot_writes,
    slot_owner,
)
from tests.test_pack_load import (  # noqa: F401 — 기존 픽스처 재사용
    _write_jsonl,
    live,
    pack_sql,
)

# ───────────────────────── 전용 더블 ─────────────────────────

class _UnenumerableVecWithLookup:
    """`_vec_shape` 가 인식 못 하는(kind=None) 커스텀 벡터 백엔드이지만
    `get_by_id` 단건 조회는 지원한다 — #332 가 다루는 정확한 형태.

    `upsert_texts` 는 3스토어(``sqlite_vec_store``/``chroma_store``/
    ``pg_vector_store``)가 공용하는 [#197] 소유권 게이트 함수를 그대로 가져와
    쓴다 — 손으로 짠 검사는 예외 문구에 남의 팩 이름을 노출해 불변식을 어긴다
    (design_v3 결함 5). 실제 게이트를 재사용하면 메시지 계약도 실제 스토어와
    자동으로 동일해진다.
    """

    def __init__(self, *, available: bool = True):
        self.available = available
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[int, list[str]]] = []  # upsert_texts 호출 기록(회귀4)
        self.get_by_id_calls: list[str] = []  # 단건 조회 호출 기록(회귀4)

    def get_by_id(self, doc_id):
        self.get_by_id_calls.append(doc_id)
        return self.rows.get(doc_id)

    def upsert_texts(self, texts, ids=None, metadatas=None):
        ids = list(ids or [])
        metadatas = default_metadatas(texts, metadatas)
        reject_batch_pack_conflicts(ids, metadatas)
        existing_owners = {
            i: slot_owner(self.rows[i].get("metadata"))
            for i in ids if i in self.rows
        }
        reject_foreign_slot_writes(ids, metadatas, existing_owners)
        self.calls.append((len(ids), list(ids)))
        for i, m in zip(ids, metadatas):
            self.rows[i] = {"metadata": dict(m or {})}

    def delete(self, ids):
        for i in ids:
            self.rows.pop(i, None)


class _RaisingLookupVec(_UnenumerableVecWithLookup):
    """단건 조회가 지정한 id 에서만 예외를 던진다(회귀6 — 셋째 분기 전용)."""

    def __init__(self, *, raise_for: str, **kw):
        super().__init__(**kw)
        self._raise_for = raise_for

    def get_by_id(self, doc_id):
        self.get_by_id_calls.append(doc_id)
        if doc_id == self._raise_for:
            raise RuntimeError("고의 조회 실패(회귀6)")
        return self.rows.get(doc_id)


def _chunk_row(chunk_id: str, text: str = "본문", **meta) -> dict:
    return {"id": chunk_id, "document_id": chunk_id, "text": text, "metadata": meta}


def _live_chunks_from_docs(docs) -> dict[str, tuple[str, dict]]:
    return {sid: (txt, json.loads(md)) for sid, txt, md in docs._conn.execute(
        "SELECT source_id, text, metadata FROM doc_sources")}


def _summary_records(caplog) -> list:
    """세 분기 요약 로그(``벡터 유실 회수 미확인(...)``)만 골라낸다 — 청크별
    즉시 경고(``청크 단건 벡터 조회 오류`` 등)와 섞이지 않게 한다."""
    return [r for r in caplog.records if "벡터 유실 회수 미확인(" in r.getMessage()]


# ───────────────────────── RED 1 — opt-out 기본값 ─────────────────────────

class TestOptOutDefaultPreservesBehaviorButFlagsIt:
    def test_default_leaves_it_unrecovered_but_counted_and_logged(
            self, live, tmp_path, pack_sql, caplog):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        vec = _UnenumerableVecWithLookup()
        pack_load.load_chunks("pack-1", f, vec, docs, sql=pack_sql)
        live_chunks = _live_chunks_from_docs(docs)
        del vec.rows["c1"]  # 벡터만 유실(부분 삭제) 재현 — doc 은 그대로

        with caplog.at_level("WARNING", logger="opencrab.pack.load"):
            c_new, c_txt, c_meta, c_same, err, ids, vec_unrecovered = (
                pack_load.load_chunks_incremental(
                    "pack-1", f, vec, docs, live_chunks, sql=pack_sql))
        assert (c_new, c_txt, c_meta, c_same, err) == (0, 0, 0, 1, 0), (
            "recover_vectors 기본값(False)에서 카운트 산출 알고리즘이 #332 이전과 "
            f"달라졌다: {(c_new, c_txt, c_meta, c_same, err)}")
        assert vec_unrecovered == 1, "열거 불가 + opt-out 이면 미확인 건수로 드러나야 한다"
        assert vec.get_by_id_calls == [], "opt-out 인데 단건 조회를 시도했다"

        summary = _summary_records(caplog)
        assert len(summary) == 1, f"opt-out 요약 로그는 정확히 1건이어야 한다: {[r.getMessage() for r in caplog.records]}"
        msg = summary[0].getMessage()
        assert "recover_vectors=True 로 단건 조회 회수를 켤 수 있다" in msg, msg


# ───────────────────────── RED 2 — 옵트인이 실제로 회수한다 ─────────────────

class TestRecoverVectorsTrueActuallyRecovers:
    def test_opt_in_recovers_the_lost_vector_then_converges_to_same(
            self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        vec = _UnenumerableVecWithLookup()
        pack_load.load_chunks("pack-1", f, vec, docs, sql=pack_sql)
        live_chunks = _live_chunks_from_docs(docs)
        del vec.rows["c1"]  # 벡터만 유실 재현
        vec.calls.clear()  # 최초 적재 호출 기록은 회수 검증과 무관하다

        c_new, c_txt, c_meta, c_same, err, ids, vec_unrecovered = (
            pack_load.load_chunks_incremental(
                "pack-1", f, vec, docs, live_chunks, sql=pack_sql,
                recover_vectors=True))
        assert (c_new, c_txt, c_meta, c_same, err) == (0, 1, 0, 0, 0), (
            f"단건 조회 회수가 텍스트 경로로 재임베딩시키지 않았다: "
            f"{(c_new, c_txt, c_meta, c_same, err)}")
        assert vec_unrecovered == 0, "회수됐으면 미확인 건수는 0 이어야 한다"
        assert "c1" in vec.rows, "벡터가 실제로 회수되지 않았다"
        assert vec.get_by_id_calls == ["c1"], "단건 조회가 정확히 1회 호출돼야 한다"

        # 2회차: 이제 벡터가 있으니 same 으로 수렴한다(재조회도 없어야 한다).
        live_chunks2 = _live_chunks_from_docs(docs)
        vec.get_by_id_calls.clear()
        vec.calls.clear()
        c_new2, c_txt2, c_meta2, c_same2, err2, _ids2, vu2 = (
            pack_load.load_chunks_incremental(
                "pack-1", f, vec, docs, live_chunks2, sql=pack_sql,
                recover_vectors=True))
        assert (c_new2, c_txt2, c_meta2, c_same2, err2, vu2) == (0, 0, 0, 1, 0, 0), (
            f"2회차가 same 으로 수렴하지 않았다: "
            f"{(c_new2, c_txt2, c_meta2, c_same2, err2, vu2)}")
        assert vec.get_by_id_calls == ["c1"], (
            "vec_set 이 아니라 vec 인식 자체가 바뀐 게 아니므로 2회차도 여전히 "
            "단건 조회를 거쳐야 한다(열거 가능해진 게 아니다) — 존재가 확인되면 "
            "재임베딩은 다시 안 일어나야 한다")
        assert vec.calls == [], "2회차에 재임베딩이 또 일어났다"


# ───────────────────────── RED 3 — 남의 팩 슬롯 충돌은 실제 게이트가 거부 ──

class TestForeignSlotConflictIsRejectedByTheRealGate:
    def test_recovery_into_a_foreign_owned_slot_is_rejected_and_keeps_the_owners_row(
            self, live, tmp_path, pack_sql):
        """단건 조회가 "존재"를 확인해도 그 슬롯이 다른 팩 소유면 회수 경로가
        그 슬롯을 가로채면 안 된다 — `slot_owner()` 대조 없이 `hit is None`
        만 봤다면(design_v3 이전 초안의 결함) 이 시나리오를 "존재"로 오판해
        c_same 처리하고 유실은 계속 가려진다. `slot_owner()` 로 소유자가
        다름을 확인하면 회수 배치로 보내되, 실제 쓰기는 [#197] 게이트
        (`reject_foreign_slot_writes`)가 그 자리에서 거부해야 한다 — 손으로
        짠 예외가 아니라 3스토어와 동일한 계약이다. 예외 문구는 검사하지
        않는다(실제 스토어도 문구를 안 본다, 카운트와 행 보존만 본다)."""
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        # 최초 적재는 슬롯 충돌과 무관한 별도 인스턴스로 정상 완주시킨다 — pack-1
        # 자신의 doc 행이 실제로 존재해야 "텍스트·메타 동일" 경로로 들어간다.
        # (주의: `available=False`인 더블을 쓰면 `load_chunks` 가 청크 전체를
        # skip 해 doc 행 자체가 생기지 않는다 — 그러면 다음 청크가 `live is None`
        # 인 "신규" 경로로 빠져 이 테스트가 의도한 회수 분기를 전혀 지나가지
        # 않는다. 처음엔 이 실수로 mutation 1을 못 잡았다.)
        pack_load.load_chunks("pack-1", f, _UnenumerableVecWithLookup(), docs, sql=pack_sql)
        live_chunks = _live_chunks_from_docs(docs)  # pack-1 자신의 doc 은 정상 존재

        vec = _UnenumerableVecWithLookup()
        vec.rows["c1"] = {"metadata": {"pack_id": "다른팩"}}  # 이미 다른 팩이 점유한 슬롯
        c_new, c_txt, c_meta, c_same, err, ids, vec_unrecovered = (
            pack_load.load_chunks_incremental(
                "pack-1", f, vec, docs, live_chunks, sql=pack_sql,
                recover_vectors=True))
        assert (c_txt, err) == (0, 1), (
            f"남의 팩 슬롯 충돌이 조용히 회수되거나 조용히 사라졌다: "
            f"c_txt={c_txt} err={err}")
        assert vec.rows["c1"] == {"metadata": {"pack_id": "다른팩"}}, (
            "거부된 시도가 남의 팩 행을 건드렸다")


# ───────────────────────── RED 4 — 단건 조회는 청크별로 개별 호출된다 ──────

class TestLookupFallbackIsPerChunkNotBatchWide:
    def test_three_consecutive_losses_trigger_three_separate_lookups(
            self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        rows = [_chunk_row("c1"), _chunk_row("c2"), _chunk_row("c3")]
        f = _write_jsonl(tmp_path / "c.jsonl", rows)
        vec = _UnenumerableVecWithLookup()
        pack_load.load_chunks("pack-1", f, vec, docs, sql=pack_sql)
        live_chunks = _live_chunks_from_docs(docs)
        for cid in ("c1", "c2", "c3"):
            del vec.rows[cid]  # 셋 다 벡터만 유실
        vec.calls.clear()  # 최초 적재 호출 기록은 회수 검증과 무관하다

        c_new, c_txt, c_meta, c_same, err, ids, vec_unrecovered = (
            pack_load.load_chunks_incremental(
                "pack-1", f, vec, docs, live_chunks, sql=pack_sql,
                recover_vectors=True, batch_size=1))
        assert (c_txt, c_same, err, vec_unrecovered) == (3, 0, 0, 0), (
            f"batch_size=1 에서도 3건 전부 회수돼야 한다: "
            f"{(c_txt, c_same, err, vec_unrecovered)}")
        assert sorted(vec.get_by_id_calls) == ["c1", "c2", "c3"], (
            "단건 조회가 청크마다 개별 호출되지 않았다(배치 전체를 한 번에 판단했다면 "
            f"이 목록이 달라진다): {vec.get_by_id_calls}")
        assert len(vec.calls) == 3, (
            f"batch_size=1 이면 회수 upsert 도 청크별로 갈라져야 한다: {vec.calls}")


# ───────────────────────── RED 5 — 둘째 분기(조회 수단 없음/비활성) ────────

class TestUnavailableBackendSkipsLookupAndSaysSo:
    def test_unavailable_backend_logs_the_no_lookup_branch_not_the_opt_out_one(
            self, live, tmp_path, pack_sql, caplog):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        vec = _UnenumerableVecWithLookup(available=True)
        pack_load.load_chunks("pack-1", f, vec, docs, sql=pack_sql)
        live_chunks = _live_chunks_from_docs(docs)
        del vec.rows["c1"]
        vec.available = False  # 백엔드가 이제 비활성 — get_by_id 수단이 있어도 안 쓴다

        with caplog.at_level("WARNING", logger="opencrab.pack.load"):
            c_new, c_txt, c_meta, c_same, err, ids, vec_unrecovered = (
                pack_load.load_chunks_incremental(
                    "pack-1", f, vec, docs, live_chunks, sql=pack_sql,
                    recover_vectors=True))
        assert (c_txt, c_same, vec_unrecovered) == (0, 1, 1), (
            f"비활성 백엔드에서 조회를 시도했거나 미확인으로 안 남겼다: "
            f"{(c_txt, c_same, vec_unrecovered)}")
        assert vec.get_by_id_calls == [], "비활성인데 단건 조회를 시도했다"

        summary = _summary_records(caplog)
        assert len(summary) == 1, f"둘째 분기 요약 로그는 정확히 1건이어야 한다: {[r.getMessage() for r in caplog.records]}"
        msg = summary[0].getMessage()
        assert "지원하지 않거나" in msg, msg
        assert "켤 수 있다" not in msg, (
            f"둘째 분기인데 첫째 분기(opt-out 안내) 문구가 새어 들어갔다: {msg}")


# ───────────────────────── RED 6 — 셋째 분기(단건 조회 예외) ───────────────

class TestLookupExceptionLogsTheErrorBranchNotTheOtherTwo:
    def test_get_by_id_raising_is_counted_and_logs_the_error_branch(
            self, live, tmp_path, pack_sql, caplog):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "c.jsonl", [_chunk_row("c1")])
        seed_vec = _UnenumerableVecWithLookup()
        pack_load.load_chunks("pack-1", f, seed_vec, docs, sql=pack_sql)
        live_chunks = _live_chunks_from_docs(docs)

        vec = _RaisingLookupVec(raise_for="c1")
        vec.rows.update(seed_vec.rows)  # 조회 예외이지 부재가 아니라는 전제 유지

        with caplog.at_level("WARNING", logger="opencrab.pack.load"):
            c_new, c_txt, c_meta, c_same, err, ids, vec_unrecovered = (
                pack_load.load_chunks_incremental(
                    "pack-1", f, vec, docs, live_chunks, sql=pack_sql,
                    recover_vectors=True))
        assert (c_txt, c_same, vec_unrecovered) == (0, 1, 1), (
            f"조회 예외가 회수로 오판되거나 미확인으로 안 남았다: "
            f"{(c_txt, c_same, vec_unrecovered)}")
        assert vec.get_by_id_calls == ["c1"], (
            "단건 조회 자체가 실제로 호출되지 않았다 — 요약 로그의 분기 선택은 "
            "루프 진입 여부와 무관하게 사전 판정된 vec_get_by_id 유무만 보므로, "
            "이 단언이 없으면 회수 분기 코드 자체를 지워도 못 잡는다")

        summary = _summary_records(caplog)
        assert len(summary) == 1, (
            "셋째 분기 요약 로그는(청크별 즉시 경고와 섞이지 않고) 정확히 1건이어야 "
            f"한다: {[r.getMessage() for r in caplog.records]}")
        msg = summary[0].getMessage()
        assert "조회 오류" in msg, msg
        assert "지원하지 않거나" not in msg, f"둘째 분기 문구가 새어 들어갔다: {msg}"
        assert "켤 수 있다" not in msg, f"첫째 분기 문구가 새어 들어갔다: {msg}"
