"""#110 단계 B: 청크 적재기의 space 결정 계약.

`load_chunks`(전량)는 유효한 원본 space, 없으면 evidence 를 쓴다. 라이브 값을
읽지 않으므로 기존 레코드 위의 전량 재적재는 라이브의 비기본 space 를 이 결과로
바꾼다. `load_chunks_incremental`(증분)은 유효한 원본 space, 없으면 유효한 라이브
space, 없으면 evidence 를 쓴다. 증분 적재기만 라이브의 유효한 space 를 보존한다.

결정기는 원본 row 를 읽고 변환 사본의 문자열화된 space 는 참조하지 않는다.
기본 시험은 진짜 SqliteVecStore 와 SQLite 문서 스토어를 쓴다. 실패 주입 시험만
메타를 기억하는 벡터 더블을 쓴다. 기존 픽스처와 더블은 `tests/test_pack_load*.py`
에서 재사용한다.
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


# 무효 원본: falsy 값과 truthy 비문자열 값. 후자는 truthiness 판정 변이를 잡는다.
_BAD_RAW = [[], {}, 0, False, None, "", 5, True, 1.5, ["concept"], {"a": 1}]


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
        rows = [_row("c1"), _row("c2", space="resource")]
        rows += [_row(f"b{i}", space=bad) for i, bad in enumerate(_BAD_RAW)]
        vec, docs = _seed(live, tmp_path, pack_sql, rows)
        assert _space_of(vec, docs, "c1") == "evidence"
        assert _space_of(vec, docs, "c2") == "resource"
        for i in range(len(_BAD_RAW)):
            assert _space_of(vec, docs, f"b{i}") == "evidence"

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

    @pytest.mark.parametrize("bad", _BAD_RAW)
    def test_invalid_raw_resolves_by_live_then_evidence(self, bad):
        row = _row("c1", space=bad)
        assert resolve_chunk_space(row) == "evidence"
        assert resolve_chunk_space(row, {"space": 5}) == "evidence"
        assert resolve_chunk_space(row, {"space": "concept"}) == "concept"

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


def test_transform_chunk_meta_does_not_mutate_the_row():
    """R: 결정기가 원본을 본다는 전제, 곧 변환이 입력 행을 바꾸지 않는다."""
    import copy
    row = {"id": "c1", "document_id": "d", "text": "t",
           "metadata": {"space": [], "source": "orig", "k": {"a": 1}}}
    before = copy.deepcopy(row)
    out = transform_chunk_meta(PACK, row)
    assert row == before
    assert out["space"] == "[]"          # 변환 사본은 문자열화돼 있다
    assert resolve_chunk_space(row) == "evidence"


class TestRealSqliteVecStore:
    """B1: 진짜 SqliteVecStore(vec0 SQL 갱신 경로 포함)와 문서 스토어로 보존을 확인한다."""

    @pytest.fixture
    def rvec(self, tmp_path):
        from tests._vec_helpers import build_vector_store
        return build_vector_store("sqlite-vec", tmp_path)

    @staticmethod
    def _vs(vec, cid):
        return vec.get_by_id(cid)["metadata"]

    def _both(self, vec, docs, cid):
        v, d = self._vs(vec, cid).get("space"), _doc_meta(docs, cid).get("space")
        assert v == d, (v, d)
        return v

    def _seed(self, live, tmp_path, pack_sql, rvec, rows):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "seed.jsonl", rows)
        ok, err = pack_load.load_chunks(PACK, f, rvec, docs, sql=pack_sql)
        assert (ok, err) == (len(rows), 0)
        return docs

    def test_full_loader_default_and_explicit(self, live, tmp_path, pack_sql, rvec):
        rows = [_row("c1"), _row("c2", space="resource")]
        rows += [_row(f"b{i}", space=bad) for i, bad in enumerate(_BAD_RAW)]
        docs = self._seed(live, tmp_path, pack_sql, rvec, rows)
        assert self._both(rvec, docs, "c1") == "evidence"
        assert self._both(rvec, docs, "c2") == "resource"
        for i in range(len(_BAD_RAW)):
            assert self._both(rvec, docs, f"b{i}") == "evidence"

    def test_full_reload_over_live_concept_replaces_it_with_evidence(
            self, live, tmp_path, pack_sql, rvec):
        """문서가 적은 전량 적재기 계약을 고정한다: 라이브 값을 읽지 않는다."""
        docs = self._seed(live, tmp_path, pack_sql, rvec,
                          [_row("c1", space="concept")])
        assert self._both(rvec, docs, "c1") == "concept"
        f = _write_jsonl(tmp_path / "again.jsonl", [_row("c1")])
        assert pack_load.load_chunks(PACK, f, rvec, docs, sql=pack_sql) == (1, 0)
        assert self._both(rvec, docs, "c1") == "evidence"

    @pytest.mark.parametrize("bad", _BAD_RAW + ["__missing__"])
    def test_invalid_raw_keeps_live_concept_without_writes(
            self, live, tmp_path, pack_sql, rvec, bad):
        docs = self._seed(live, tmp_path, pack_sql, rvec,
                          [_row("c1", space="concept")])
        row = _row("c1") if bad == "__missing__" else _row("c1", space=bad)
        res = _incr(tmp_path, pack_sql, rvec, docs, [row])
        assert _counts(res) == (0, 0, 0, 1, 0)
        assert self._both(rvec, docs, "c1") == "concept"

    def test_meta_only_change_runs_real_sql_update_and_keeps_concept(
            self, live, tmp_path, pack_sql, rvec):
        docs = self._seed(live, tmp_path, pack_sql, rvec,
                          [_row("c1", space="concept")])
        res = _incr(tmp_path, pack_sql, rvec, docs, [_row("c1", space=[], 쪽="3")])
        assert _counts(res) == (0, 0, 1, 0, 0)
        assert self._both(rvec, docs, "c1") == "concept"
        assert self._vs(rvec, "c1")["쪽"] == "3"
        assert _doc_meta(docs, "c1")["쪽"] == "3"

    def test_text_change_keeps_live_concept(self, live, tmp_path, pack_sql, rvec):
        docs = self._seed(live, tmp_path, pack_sql, rvec,
                          [_row("c1", space="concept")])
        res = _incr(tmp_path, pack_sql, rvec, docs, [_row("c1", "다른 본문")])
        assert _counts(res) == (0, 1, 0, 0, 0)
        assert self._both(rvec, docs, "c1") == "concept"

    def test_untagged_legacy_gets_evidence_once_then_same(
            self, live, tmp_path, pack_sql, rvec):
        _b, _g, docs = live
        legacy = transform_chunk_meta(PACK, _row("c1"))
        rvec.upsert_texts(texts=["본문"], metadatas=[legacy], ids=["c1"])
        docs.upsert_source("c1", "본문", legacy)
        res = _incr(tmp_path, pack_sql, rvec, docs, [_row("c1")])
        assert _counts(res) == (0, 0, 1, 0, 0)
        assert self._both(rvec, docs, "c1") == "evidence"
        res2 = _incr(tmp_path, pack_sql, rvec, docs, [_row("c1")], name="again")
        assert _counts(res2) == (0, 0, 0, 1, 0)

    @pytest.mark.parametrize("live_space", [5, True, ["concept"], {"a": 1}])
    def test_invalid_live_space_becomes_evidence(
            self, live, tmp_path, pack_sql, rvec, live_space):
        _b, _g, docs = live
        bad = transform_chunk_meta(PACK, _row("c1"))
        bad["space"] = live_space
        rvec.upsert_texts(texts=["본문"], metadatas=[bad], ids=["c1"])
        docs.upsert_source("c1", "본문", bad)
        _incr(tmp_path, pack_sql, rvec, docs, [_row("c1")])
        assert self._both(rvec, docs, "c1") == "evidence"
