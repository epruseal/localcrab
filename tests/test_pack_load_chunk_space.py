"""#110 단계 B: 두 청크 적재기는 재적재 때 유효한 space 를 지우지 않는다.

결정 순서는 원본 metadata 의 유효한 space, 라이브 metadata 의 유효한 space,
evidence 다. 원본 판정은 `chroma_safe_meta` 가 list/dict 를 문자열로 바꾸기
전에 한다. 시험은 진짜 SQLite 문서 스토어와 메타를 기억하는 벡터 더블을
쓴다. 기존 픽스처와 더블은 `tests/test_pack_load*.py` 에서 재사용한다.
"""
from __future__ import annotations

import json

import pytest

from opencrab.pack import load as pack_load
from opencrab.pack.normalize import resolve_chunk_space, transform_chunk_meta
from tests.test_pack_load import (  # noqa: F401 - 기존 픽스처 재사용
    _write_jsonl,
    live,
    pack_sql,
)
from tests.test_pack_load_r12_selfheal_gates import (
    _chunk_row,
    _EnumerableVec,
    _live_chunks_from_docs,
)
from tests.test_pack_load_r14_unenumerable_vec_gates import (
    _UnenumerableVecWithLookup,
)

PACK = "pack-1"


class _MetaVec(_EnumerableVec):
    """열거 가능하고 메타를 기억하는 더블. 실패 모드는 속성으로 켠다."""

    def __init__(self):
        super().__init__(PACK)
        self.meta: dict[str, dict] = {}
        self.update_mode = "ok"      # ok | false | raise_inside
        self.fail_batch = False      # 2건 이상 배치 upsert 를 실패시킨다

    def upsert_texts(self, texts, ids=None, metadatas=None):
        ids = list(ids or [])
        if self.fail_batch and len(ids) > 1:
            raise RuntimeError("고의 배치 실패")
        super().upsert_texts(texts, ids, metadatas)
        for i, m in zip(ids, metadatas):
            self.meta[i] = dict(m)

    def update_metadata(self, chunk_id, meta):
        if self.update_mode == "false":
            return False
        if self.update_mode == "raise_inside":
            raise RuntimeError("고의 갱신 실패")
        if chunk_id not in self.meta:
            return False
        self.meta[chunk_id] = dict(meta)
        return True


def _doc_meta(docs, cid):
    row = docs._conn.execute(
        "SELECT metadata FROM doc_sources WHERE source_id = ?", (cid,)).fetchone()
    return json.loads(row[0])


def _space_of(vec, docs, cid):
    """두 스토어의 space 가 같음을 확인하고 그 값을 돌려준다(D)."""
    md = vec.meta[cid] if isinstance(vec, _MetaVec) else vec.rows[cid]["metadata"]
    dm = _doc_meta(docs, cid)
    assert md.get("space") == dm.get("space"), (md.get("space"), dm.get("space"))
    return dm.get("space")


def _row(cid, text="본문", **meta):
    return _chunk_row(cid, text, **meta)


def _seed(live, tmp_path, pack_sql, rows, vec=None, name="seed"):
    """첫 적재로 라이브 기준선을 만든다."""
    _b, _g, docs = live
    vec = vec or _MetaVec()
    f = _write_jsonl(tmp_path / f"{name}.jsonl", rows)
    pack_load.load_chunks(PACK, f, vec, docs, sql=pack_sql)
    return vec, docs


def _incr(tmp_path, pack_sql, vec, docs, rows, name="next", **kw):
    f = _write_jsonl(tmp_path / f"{name}.jsonl", rows)
    return pack_load.load_chunks_incremental(
        PACK, f, vec, docs, _live_chunks_from_docs(docs), sql=pack_sql, **kw)


def _counts(res):
    c_new, c_txt, c_meta, c_same, err = res[:5]
    return c_new, c_txt, c_meta, c_same, err


_BAD_RAW = [[], {}, 0, False, None, ""]


class TestRawValidity:
    """A: 무효 원본 값은 라이브 concept 를 지우지 못한다."""

    @pytest.mark.parametrize("bad", _BAD_RAW + ["__missing__"])
    def test_invalid_raw_keeps_live_concept(self, live, tmp_path, pack_sql, bad):
        vec, docs = _seed(live, tmp_path, pack_sql, [_row("c1", space="concept")])
        row = _row("c1") if bad == "__missing__" else _row("c1", space=bad)
        res = _incr(tmp_path, pack_sql, vec, docs, [row])
        assert _counts(res) == (0, 0, 0, 1, 0)
        assert _space_of(vec, docs, "c1") == "concept"

    def test_invalid_raw_with_other_meta_change_keeps_concept(
            self, live, tmp_path, pack_sql):
        vec, docs = _seed(live, tmp_path, pack_sql, [_row("c1", space="concept")])
        res = _incr(tmp_path, pack_sql, vec, docs, [_row("c1", space=[], 쪽="3")])
        assert _counts(res) == (0, 0, 1, 0, 0)
        assert _space_of(vec, docs, "c1") == "concept"
        assert vec.meta["c1"]["쪽"] == "3"

    def test_valid_raw_wins_over_live(self, live, tmp_path, pack_sql):
        vec, docs = _seed(live, tmp_path, pack_sql, [_row("c1", space="concept")])
        res = _incr(tmp_path, pack_sql, vec, docs, [_row("c1", space="resource")])
        assert _counts(res) == (0, 0, 1, 0, 0)
        assert _space_of(vec, docs, "c1") == "resource"


class TestBranchTable:
    """B: 분기마다 같은 결정 meta 를 쓴다."""

    def test_full_loader_defaults_to_evidence_and_keeps_explicit(
            self, live, tmp_path, pack_sql):
        vec, docs = _seed(live, tmp_path, pack_sql,
                          [_row("c1"), _row("c2", space="resource"),
                           _row("c3", space=[])])
        assert _space_of(vec, docs, "c1") == "evidence"
        assert _space_of(vec, docs, "c2") == "resource"
        assert _space_of(vec, docs, "c3") == "evidence"

    def test_incremental_new_chunk_defaults_to_evidence(
            self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        vec = _MetaVec()
        res = _incr(tmp_path, pack_sql, vec, docs, [_row("c1")])
        assert _counts(res) == (1, 0, 0, 0, 0)
        assert _space_of(vec, docs, "c1") == "evidence"

    def test_unchanged_live_concept_is_same_without_writes(
            self, live, tmp_path, pack_sql):
        vec, docs = _seed(live, tmp_path, pack_sql, [_row("c1", space="concept")])
        calls = len(vec.upsert_calls)
        res = _incr(tmp_path, pack_sql, vec, docs, [_row("c1")])
        assert _counts(res) == (0, 0, 0, 1, 0)
        assert len(vec.upsert_calls) == calls

    def test_text_change_keeps_live_concept(self, live, tmp_path, pack_sql):
        vec, docs = _seed(live, tmp_path, pack_sql, [_row("c1", space="concept")])
        res = _incr(tmp_path, pack_sql, vec, docs, [_row("c1", "다른 본문")])
        assert _counts(res) == (0, 1, 0, 0, 0)
        assert _space_of(vec, docs, "c1") == "concept"

    def test_vector_loss_recovery_keeps_live_concept(self, live, tmp_path, pack_sql):
        vec, docs = _seed(live, tmp_path, pack_sql, [_row("c1", space="concept")])
        vec2 = _MetaVec()  # 벡터 축 전체 유실, doc 은 그대로
        res = _incr(tmp_path, pack_sql, vec2, docs, [_row("c1")])
        assert _counts(res) == (0, 1, 0, 0, 0)
        assert _space_of(vec2, docs, "c1") == "concept"

    def test_untagged_legacy_gets_one_meta_update_then_same(
            self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        vec = _MetaVec()
        legacy = transform_chunk_meta(PACK, _row("c1"))
        assert "space" not in legacy
        vec.upsert_texts(["본문"], ["c1"], [legacy])
        docs.upsert_source("c1", "본문", legacy)
        res = _incr(tmp_path, pack_sql, vec, docs, [_row("c1")])
        assert _counts(res) == (0, 0, 1, 0, 0)
        assert _space_of(vec, docs, "c1") == "evidence"
        res2 = _incr(tmp_path, pack_sql, vec, docs, [_row("c1")], name="again")
        assert _counts(res2) == (0, 0, 0, 1, 0)


class TestMetaUpdateFailure:
    """C: 비 space 메타 변경으로 메타 전용 분기에 들어간 뒤 갱신이 실패한다."""

    @pytest.mark.parametrize("mode", ["false", "raise_inside"])
    def test_false_return_requeues_with_live_space(
            self, live, tmp_path, pack_sql, mode):
        vec, docs = _seed(live, tmp_path, pack_sql, [_row("c1", space="concept")])
        vec.update_mode = mode
        res = _incr(tmp_path, pack_sql, vec, docs, [_row("c1", 쪽="3")])
        assert _counts(res) == (0, 1, 0, 0, 0)
        assert _space_of(vec, docs, "c1") == "concept"
        assert vec.meta["c1"]["쪽"] == "3"

    def test_propagating_exception_only_counts_err_then_converges(
            self, live, tmp_path, pack_sql, monkeypatch):
        vec, docs = _seed(live, tmp_path, pack_sql, [_row("c1", space="concept")])
        before_vec, before_doc = dict(vec.meta["c1"]), _doc_meta(docs, "c1")
        calls = len(vec.upsert_calls)

        def boom(*a, **k):
            raise RuntimeError("고의 전파")

        monkeypatch.setattr(pack_load, "_vec_meta_update", boom)
        res = _incr(tmp_path, pack_sql, vec, docs, [_row("c1", 쪽="3")])
        assert _counts(res) == (0, 0, 0, 0, 1)
        assert len(vec.upsert_calls) == calls
        assert vec.meta["c1"] == before_vec and _doc_meta(docs, "c1") == before_doc
        monkeypatch.undo()
        res2 = _incr(tmp_path, pack_sql, vec, docs, [_row("c1", 쪽="3")], name="again")
        assert _counts(res2) == (0, 0, 1, 0, 0)
        assert _space_of(vec, docs, "c1") == "concept"
        assert vec.meta["c1"]["쪽"] == "3"


class TestInvalidLiveSpace:
    """H: 유효한 원본이 없고 라이브 값이 무효이면 evidence 다."""

    @pytest.mark.parametrize("live_space", [5, True, ["concept"], {"a": 1}])
    def test_resolver_and_stores_fall_back_to_evidence(
            self, live, tmp_path, pack_sql, live_space):
        assert resolve_chunk_space(_row("c1"), {"space": live_space}) == "evidence"
        _b, _g, docs = live
        vec = _MetaVec()
        bad = transform_chunk_meta(PACK, _row("c1"))
        bad["space"] = live_space
        vec.upsert_texts(["본문"], ["c1"], [bad])
        docs.upsert_source("c1", "본문", bad)
        _incr(tmp_path, pack_sql, vec, docs, [_row("c1")])
        assert _space_of(vec, docs, "c1") == "evidence"

    @pytest.mark.parametrize("live_meta", [{"space": ""}, {}, None])
    def test_empty_or_missing_live_space_is_evidence(self, live_meta):
        assert resolve_chunk_space(_row("c1"), live_meta) == "evidence"


class TestFlushPaths:
    """E: 배치 실패 뒤 단건 재시도와 열거 불가 단건 복구가 실제로 돈다."""

    def test_full_loader_single_retry_after_batch_failure(
            self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        vec = _MetaVec()
        vec.fail_batch = True
        f = _write_jsonl(tmp_path / "c.jsonl", [_row("c1"), _row("c2", space="resource")])
        ok, err = pack_load.load_chunks(PACK, f, vec, docs, sql=pack_sql)
        assert (ok, err) == (2, 0)
        assert all(len(c) == 1 for c in vec.upsert_calls)
        assert _space_of(vec, docs, "c1") == "evidence"
        assert _space_of(vec, docs, "c2") == "resource"

    def test_incremental_single_retry_after_batch_failure(
            self, live, tmp_path, pack_sql):
        vec, docs = _seed(live, tmp_path, pack_sql,
                          [_row("c1", space="concept"), _row("c2", space="concept")])
        vec.fail_batch = True
        vec.upsert_calls.clear()
        res = _incr(tmp_path, pack_sql, vec, docs,
                    [_row("c1", "새 1"), _row("c2", "새 2")])
        assert _counts(res) == (0, 2, 0, 0, 0)
        assert all(len(c) == 1 for c in vec.upsert_calls) and vec.upsert_calls
        assert _space_of(vec, docs, "c1") == "concept"
        assert _space_of(vec, docs, "c2") == "concept"

    def test_unenumerable_single_lookup_recovery_keeps_live_space(
            self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        vec = _UnenumerableVecWithLookup()
        f1 = _write_jsonl(tmp_path / "s.jsonl", [_row("c1", space="concept")])
        pack_load.load_chunks(PACK, f1, vec, docs, sql=pack_sql)
        del vec.rows["c1"]
        res = _incr(tmp_path, pack_sql, vec, docs, [_row("c1")],
                    recover_vectors=True)
        assert _counts(res) == (0, 1, 0, 0, 0)
        assert vec.get_by_id_calls == ["c1"]
        assert _space_of(vec, docs, "c1") == "concept"


class TestResolverReachesEveryBoundary:
    """E: 모든 쓰기 경계가 결정기의 반환값을 그대로 받는다."""

    def test_sentinel_reaches_stores_in_both_loaders(
            self, live, tmp_path, pack_sql, monkeypatch):
        monkeypatch.setattr(pack_load, "resolve_chunk_space",
                            lambda row, live_meta=None: "SENTINEL")
        _b, _g, docs = live
        vec = _MetaVec()
        f = _write_jsonl(tmp_path / "f.jsonl", [_row("c1"), _row("c2"), _row("c3")])
        pack_load.load_chunks(PACK, f, vec, docs, sql=pack_sql)
        assert {_space_of(vec, docs, c) for c in ("c1", "c2", "c3")} == {"SENTINEL"}
        # 증분: c1 텍스트 변경, c2 메타 전용 변경, c3 벡터 유실 회수, c4 신규
        del vec.meta["c3"]
        vec.delete(["c3"])
        res = _incr(tmp_path, pack_sql, vec, docs,
                    [_row("c1", "새"), _row("c2", 쪽="1"), _row("c3"), _row("c4")])
        assert _counts(res) == (1, 2, 1, 0, 0)
        assert {_space_of(vec, docs, c) for c in ("c1", "c2", "c3", "c4")} == {"SENTINEL"}
