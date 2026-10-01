"""#374: 증분 same 판정이 문서 sink 자신의 값(node_type 컬럼, properties)을 본다.

부분 실패(그래프 쓰기 성공, 문서 쓰기 실패) 뒤의 증분이 그래프만 보고 same 을
내면 문서 행의 값이 영구히 낡는다. 이 파일은 그 상태를 실제 add_node 경로로
만들고(문서 쓰기만 실패시킨다), 다음 증분이 문서를 고치는지, 정상 증분은
종전처럼 same 으로 건너뛰는지를 건다.

기존 픽스처는 `tests/test_pack_load.py` 의 진짜 SQLite 3스토어를 재사용한다.
"""
from __future__ import annotations

import json
import logging

import pytest

from opencrab.pack import load as pack_load
from tests.test_pack_load import (  # noqa: F401 기존 픽스처 재사용
    _node,
    _NoVec,
    _RecordingVec,
    _write_jsonl,
    live,
    pack_sql,
)


def _doc_row(docs, space: str, node_id: str):
    row = docs._conn.execute(
        "SELECT node_type, properties FROM doc_nodes WHERE space=? AND node_id=?",
        (space, node_id)).fetchone()
    return None if row is None else (row[0], json.loads(row[1]))


def _incremental(live, pack_sql, f):
    builder, graph, docs = live
    state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
    return pack_load.load_nodes_incremental(
        "pack-1", f, builder, {}, state["nodes"], graph, docs,
        state["doc_node_spaces"], sql=pack_sql)


def _counts(result):
    n_new, n_chg, n_same, skip, err = result[:5]
    return (n_new, n_chg, n_same, skip, err)


class _FailDocWrites:
    """`upsert_node_doc` 만 예외를 던지게 만든다(그래프 쓰기는 정상)."""

    def __init__(self, docs, monkeypatch):
        self.calls = 0
        def boom(*_a, **_kw):
            self.calls += 1
            raise RuntimeError("doc write down")
        monkeypatch.setattr(docs, "upsert_node_doc", boom)


class TestDocValueDriftIsHealed:
    def test_doc_node_type_drift_is_rewritten_then_converges(self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, live[0], {})
        docs._conn.execute("UPDATE doc_nodes SET node_type='Stale' WHERE node_id='n1'")
        docs._conn.commit()

        assert _counts(_incremental(live, pack_sql, f)) == (0, 1, 0, 0, 0)
        assert _doc_row(docs, "resource", "n1")[0] == "Document"
        assert _counts(_incremental(live, pack_sql, f)) == (0, 0, 1, 0, 0)

    def test_doc_properties_drift_is_rewritten_then_converges(self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1", 발행연도="2026")])
        pack_load.load_nodes("pack-1", f, live[0], {})
        _t, props = _doc_row(docs, "resource", "n1")
        props["발행연도"] = "1999"
        docs._conn.execute(
            "UPDATE doc_nodes SET properties=? WHERE node_id='n1'", (json.dumps(props),))
        docs._conn.commit()

        assert _counts(_incremental(live, pack_sql, f)) == (0, 1, 0, 0, 0)
        assert _doc_row(docs, "resource", "n1")[1]["발행연도"] == "2026"
        assert _counts(_incremental(live, pack_sql, f)) == (0, 0, 1, 0, 0)

    def test_doc_extra_field_is_removed_by_rewrite(self, live, tmp_path, pack_sql):
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, live[0], {})
        _t, props = _doc_row(docs, "resource", "n1")
        props["stray"] = "x"
        docs._conn.execute(
            "UPDATE doc_nodes SET properties=? WHERE node_id='n1'", (json.dumps(props),))
        docs._conn.commit()

        assert _counts(_incremental(live, pack_sql, f)) == (0, 1, 0, 0, 0)
        assert "stray" not in _doc_row(docs, "resource", "n1")[1]

    def test_row_deleted_after_the_snapshot_is_rewritten(self, live, tmp_path, pack_sql):
        """호출자 스냅샷(`doc_node_spaces`)은 행이 있다고 말하는데 실제 행은 그 뒤에
        지워졌다. 값을 비교할 대상이 없으므로 어긋남으로 보고 다시 쓴다."""
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, builder, {})
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        assert "n1" in state["doc_node_spaces"]
        docs._conn.execute("DELETE FROM doc_nodes WHERE node_id='n1'")
        docs._conn.commit()

        r = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, state["nodes"], graph, docs,
            state["doc_node_spaces"], sql=pack_sql)
        assert _counts(r) == (0, 1, 0, 0, 0)
        assert _doc_row(docs, "resource", "n1") is not None

    def test_array_order_drift_is_rewritten(self, live, tmp_path, pack_sql):
        """그래프와 파일은 [1, 2], 문서만 [2, 1]. 배열 순서를 무시하는 직렬화는 놓친다."""
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1", properties={"seq": [1, 2]})])
        pack_load.load_nodes("pack-1", f, live[0], {})
        _t, props = _doc_row(docs, "resource", "n1")
        props["seq"] = [2, 1]
        docs._conn.execute(
            "UPDATE doc_nodes SET properties=? WHERE node_id='n1'", (json.dumps(props),))
        docs._conn.commit()

        assert _counts(_incremental(live, pack_sql, f)) == (0, 1, 0, 0, 0)
        assert _doc_row(docs, "resource", "n1")[1]["seq"] == [1, 2]
        assert _counts(_incremental(live, pack_sql, f)) == (0, 0, 1, 0, 0)

    def test_doc_row_without_pack_id_is_repaired_by_existing_path(
            self, live, tmp_path, pack_sql):
        """pack_id 없는 유효 JSON 객체 행은 지문 맵 밖이다. 기존 doc_row_missing 경로가
        chg 로 복구한다(고정 시험, 이 이슈가 바꾸지 않는 동작)."""
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, live[0], {})
        docs._conn.execute("UPDATE doc_nodes SET properties='{}' WHERE node_id='n1'")
        docs._conn.commit()

        assert _counts(_incremental(live, pack_sql, f)) == (0, 1, 0, 0, 0)
        assert _doc_row(docs, "resource", "n1")[1]["pack_id"] == "pack-1"

    def test_malformed_doc_properties_json_keeps_existing_outcome(
            self, live, tmp_path, pack_sql):
        """손상 JSON 행은 지문 맵 밖이고 기존 경로(#415 unverifiable)가 skip 으로
        센다. 복구하지 않고 죽지도 않는다. 이 이슈가 바꾸지 않는 동작을 고정한다."""
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, live[0], {})
        docs._conn.execute("UPDATE doc_nodes SET properties='{not json' WHERE node_id='n1'")
        docs._conn.commit()

        assert _counts(_incremental(live, pack_sql, f)) == (0, 0, 0, 1, 0)


class TestRealPartialFailure:
    """그래프 쓰기 성공 + 문서 쓰기 실패를 실제 add_node 경로로 만든다."""

    def test_graph_ok_doc_failed_then_next_run_repairs_doc(
            self, live, tmp_path, monkeypatch, pack_sql):
        builder, _g, docs = live
        f1 = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1", 발행연도="2026")])
        pack_load.load_nodes("pack-1", f1, builder, {})

        f2 = _write_jsonl(tmp_path / "n2.jsonl", [_node(id="n1", 발행연도="2027")])
        with monkeypatch.context() as m:
            fail = _FailDocWrites(docs, m)
            r = _incremental(live, pack_sql, f2)
            assert fail.calls == 1
        assert _counts(r) == (0, 0, 0, 0, 1), "전제: 그래프만 갱신되고 문서 쓰기가 err 로 잡힌다"
        assert _doc_row(docs, "resource", "n1")[1]["발행연도"] == "2026", "전제: 문서가 낡았다"

        # 다음 런(문서 정상): 그래프는 이미 2027 이라 종전 로직은 same 으로 방치했다.
        assert _counts(_incremental(live, pack_sql, f2)) == (0, 1, 0, 0, 0)
        assert _doc_row(docs, "resource", "n1")[1]["발행연도"] == "2027"
        # 그 뒤에는 수렴한다.
        assert _counts(_incremental(live, pack_sql, f2)) == (0, 0, 1, 0, 0)

    def test_persistent_doc_failure_reports_every_run(
            self, live, tmp_path, monkeypatch, caplog, pack_sql):
        """#301 과의 관계: 문서 쓰기가 계속 실패하면 매 런 err 와 집계 경고가 난다."""
        builder, _g, docs = live
        f1 = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1", 발행연도="2026")])
        pack_load.load_nodes("pack-1", f1, builder, {})
        f2 = _write_jsonl(tmp_path / "n2.jsonl", [_node(id="n1", 발행연도="2027")])
        # 1회차: 그래프가 먼저 달라 평범한 chg 경로다(집계 경고 없음). 그래프만 수렴한다.
        with monkeypatch.context() as m:
            _FailDocWrites(docs, m)
            assert _counts(_incremental(live, pack_sql, f2)) == (0, 0, 0, 0, 1)

        for round_no in range(3):
            caplog.clear()
            with monkeypatch.context() as m, caplog.at_level(logging.WARNING):
                _FailDocWrites(docs, m)
                r = _incremental(live, pack_sql, f2)
            assert _counts(r) == (0, 0, 0, 0, 1), f"{round_no + 1}회차"
            agg = [x for x in caplog.records if "doc 행 값 어긋남 회수 누적" in x.getMessage()]
            assert len(agg) == 1 and "누적 1건" in agg[0].getMessage(), f"{round_no + 1}회차"
        assert _doc_row(docs, "resource", "n1")[1]["발행연도"] == "2026"


class TestControlsStaySame:
    """정상 증분은 종전처럼 same 으로 건너뛴다(오탐 재적재 0)."""

    @pytest.mark.parametrize("extra", [
        {},
        {"발행연도": "2026", "properties": {"중첩": {"a": [1, 2.5, None, True]}}},
        {"properties": {"space": "resource"}},          # #358: 중첩 properties.space
        {"properties": {"id": "n1"}},                   # #379: 중첩 properties.id
        {"properties": {"pack": "pack-1"}},             # 폐기 별칭
        {"label": "한글 é emoji \U0001F600"},
    ])
    def test_normal_incremental_is_same(self, live, tmp_path, pack_sql, extra):
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1", **extra)])
        pack_load.load_nodes("pack-1", f, live[0], {})
        assert _counts(_incremental(live, pack_sql, f)) == (0, 0, 1, 0, 0)

    def test_doc_key_order_difference_is_not_a_drift(self, live, tmp_path, pack_sql):
        """저장된 JSON 의 키 순서만 달라도 same 이다(키 정렬이 지문에 들어 있다)."""
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1", a="1", b="2")])
        pack_load.load_nodes("pack-1", f, live[0], {})
        _t, props = _doc_row(docs, "resource", "n1")
        reordered = dict(reversed(list(props.items())))
        assert list(reordered) != list(props)
        docs._conn.execute(
            "UPDATE doc_nodes SET properties=? WHERE node_id='n1'", (json.dumps(reordered),))
        docs._conn.commit()
        assert _counts(_incremental(live, pack_sql, f)) == (0, 0, 1, 0, 0)

    def test_anchor_node_stays_same(self, live, tmp_path, pack_sql):
        f = _write_jsonl(tmp_path / "n.jsonl",
                         [_node(id="dataset:foo", node_type="Dataset", space="resource")])
        pack_load.load_nodes("pack-1", f, live[0], {})
        assert _counts(_incremental(live, pack_sql, f)) == (0, 0, 1, 0, 0)

    def test_doc_owner_id_difference_alone_is_not_a_drift(self, live, tmp_path, pack_sql):
        """owner_id 는 #378 소관이다. 문서 쪽 값만 달라도 같은 수렴 검사에 걸리지 않는다."""
        _b, _g, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, live[0], {})
        _t, props = _doc_row(docs, "resource", "n1")
        props["owner_id"] = "someone-else"
        docs._conn.execute(
            "UPDATE doc_nodes SET properties=? WHERE node_id='n1'", (json.dumps(props),))
        docs._conn.commit()
        assert _counts(_incremental(live, pack_sql, f)) == (0, 0, 1, 0, 0)

    def test_many_rows_cross_batch_boundary_are_all_same(
            self, live, tmp_path, monkeypatch, pack_sql):
        """키셋 페이지 경계를 넘어도 전량 same 이고 어긋난 1건만 chg 다."""
        monkeypatch.setattr(pack_load, "_DOC_FP_BATCH", 3)
        rows = [_node(id=f"n{i:02d}", 발행연도=str(i)) for i in range(10)]
        f = _write_jsonl(tmp_path / "n.jsonl", rows)
        pack_load.load_nodes("pack-1", f, live[0], {})
        assert _counts(_incremental(live, pack_sql, f)) == (0, 0, 10, 0, 0)
        _b, _g, docs = live
        docs._conn.execute("UPDATE doc_nodes SET node_type='Stale' WHERE node_id='n07'")
        docs._conn.commit()
        assert _counts(_incremental(live, pack_sql, f)) == (0, 1, 9, 0, 0)


    def test_fingerprint_pages_are_bounded_and_cursor_advances(
            self, live, tmp_path, monkeypatch, pack_sql):
        """LIMIT 을 뺀 변이는 한 번에 전량을 읽는다. 호출마다 반환 행 수 상한과 커서
        전진을 직접 본다."""
        monkeypatch.setattr(pack_load, "_DOC_FP_BATCH", 3)
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id=f"n{i:02d}") for i in range(10)])
        pack_load.load_nodes("pack-1", f, live[0], {})
        _b, graph, docs = live
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        calls: list[tuple[str, int]] = []
        real = docs._fetch_all

        def spy(sql, params):
            rows = real(sql, params)
            if "last_node_id" in params:
                calls.append((params["last_node_id"], len(rows)))
            return rows

        monkeypatch.setattr(docs, "_fetch_all", spy)
        pack_load.load_nodes_incremental(
            "pack-1", f, live[0], {}, state["nodes"], graph, docs,
            state["doc_node_spaces"], sql=pack_sql)
        assert len(calls) >= 4, calls
        assert all(n <= 3 for _c, n in calls), calls
        cursors = [c for c, _n in calls]
        assert cursors == sorted(cursors) and len(set(cursors)) == len(cursors), calls


class TestNumericCanonicalForm:
    def _fp(self, **props):
        return pack_load._doc_content_fingerprint("Document", props)

    @pytest.mark.parametrize("a,b", [
        (1.0, 1), (1e22, 10**22), (1e300, 10**300), (-0.0, 0), (0.5, 0.50),
    ])
    def test_equal_decimal_values_share_a_fingerprint(self, a, b):
        assert self._fp(v=a) == self._fp(v=b)
        assert self._fp(v=[a, {"k": a}]) == self._fp(v=[b, {"k": b}])

    @pytest.mark.parametrize("a,b", [
        (True, 1), ("1", 1), (None, 0), (10**28, 10**28 + 1),
        (0.1, 0.10000000000000002), (-1, 1), ([1, 2], [2, 1]),
    ])
    def test_different_values_differ(self, a, b):
        assert self._fp(v=a) != self._fp(v=b)

    def test_result_does_not_depend_on_decimal_context(self):
        import decimal

        base = (self._fp(v=10**28), self._fp(v=10**28 + 1))
        with decimal.localcontext() as ctx:
            ctx.prec = 3
            ctx.traps[decimal.Inexact] = True
            assert (self._fp(v=10**28), self._fp(v=10**28 + 1)) == base
        assert base[0] != base[1]

    def test_ignored_keys_do_not_enter_the_fingerprint(self):
        assert self._fp(a=1) == self._fp(a=1, id="x", space="s", owner_id="o", pack="p")


class TestOldSpaceRecoveryWithAuditFailure:
    """#375 이관 잔여 2: 타입(과 space) 변경 + 감사 실패가 같은 런에 겹쳐도 구 space
    문서 행은 다음 런에 회수된다."""

    def test_old_space_doc_row_is_removed_on_the_next_run(
            self, live, tmp_path, monkeypatch, pack_sql):
        builder, graph, docs = live
        f1 = _write_jsonl(tmp_path / "n1.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f1, builder, {})
        assert _doc_row(docs, "resource", "n1") is not None

        f2 = _write_jsonl(tmp_path / "n2.jsonl",
                          [_node(id="n1", node_type="Agent", space="subject")])

        def audit_boom(*_a, **_kw):
            raise RuntimeError("audit down")

        with monkeypatch.context() as m:
            m.setattr(docs, "log_event", audit_boom)
            first = _incremental(live, pack_sql, f2)
        assert first[4] >= 1, "전제: 감사 실패가 err 로 잡힌다"

        second = _incremental(live, pack_sql, f2)
        assert second[4] == 0
        assert _doc_row(docs, "resource", "n1") is None, "구 space 문서 행이 남았다"
        assert _doc_row(docs, "subject", "n1") is not None
        assert _counts(_incremental(live, pack_sql, f2)) == (0, 0, 1, 0, 0)


def _poke_doc(docs, node_id: str, raw: str):
    """문서 행 properties 를 원문 그대로 바꾼다(JSON5 등 SQLite 가 받는 값)."""
    docs._conn.execute("UPDATE doc_nodes SET properties=? WHERE node_id=?", (raw, node_id))
    docs._conn.commit()


_UNREADABLE_ROWS = {
    "json5": "{pack_id:'pack-1', title:'bad'}",
    "infinity": '{"pack_id":"pack-1","v":1e400}',
    "deep": '{"pack_id":"pack-1","d":' + "[" * 800 + "]" * 800 + "}",
}


def _two_nodes(live, tmp_path, pack_sql):
    builder, _g, docs = live
    f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1"), _node(id="n2")])
    pack_load.load_nodes("pack-1", f, builder, {})
    return f, docs


def _warnings(caplog, needle):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


class TestUnreadableDocRowsNeverAbortTheRun:
    @pytest.mark.parametrize("kind", sorted(_UNREADABLE_ROWS))
    def test_unreadable_row_is_left_alone_and_neighbor_drift_is_repaired(
            self, live, tmp_path, caplog, pack_sql, kind):
        f, docs = _two_nodes(live, tmp_path, pack_sql)
        _poke_doc(docs, "n1", _UNREADABLE_ROWS[kind])
        docs._conn.execute("UPDATE doc_nodes SET node_type='Stale' WHERE node_id='n2'")
        docs._conn.commit()
        with caplog.at_level(logging.WARNING):
            r = _incremental(live, pack_sql, f)
        assert _counts(r) == (0, 1, 1, 0, 0)
        assert docs._conn.execute(
            "SELECT node_type FROM doc_nodes WHERE node_id='n2'").fetchone()[0] == "Document"
        msgs = _warnings(caplog, "해석 불가 비교 생략")
        assert len(msgs) == 1 and "1건" in msgs[0]

    def test_unreadable_row_outside_the_input_does_not_abort(self, live, tmp_path, pack_sql):
        builder, _g, docs = live
        f_all = _write_jsonl(tmp_path / "all.jsonl", [_node(id="n1"), _node(id="n2")])
        pack_load.load_nodes("pack-1", f_all, builder, {})
        _poke_doc(docs, "n2", _UNREADABLE_ROWS["json5"])
        f_one = _write_jsonl(tmp_path / "one.jsonl", [_node(id="n1")])
        assert _counts(_incremental(live, pack_sql, f_one)) == (0, 0, 1, 0, 0)

    def test_warning_counts_every_unreadable_candidate(self, live, tmp_path, caplog, pack_sql):
        f, docs = _two_nodes(live, tmp_path, pack_sql)
        _poke_doc(docs, "n1", _UNREADABLE_ROWS["json5"])
        _poke_doc(docs, "n2", _UNREADABLE_ROWS["infinity"])
        with caplog.at_level(logging.WARNING):
            assert _counts(_incremental(live, pack_sql, f)) == (0, 0, 2, 0, 0)
        msgs = _warnings(caplog, "해석 불가 비교 생략")
        assert len(msgs) == 1 and "2건" in msgs[0]

    def test_scan_cursor_advances_over_unreadable_rows(
            self, live, tmp_path, monkeypatch, pack_sql):
        monkeypatch.setattr(pack_load, "_DOC_FP_BATCH", 2)
        builder, _g, docs = live
        rows = [_node(id=f"n{i}") for i in range(5)]
        f = _write_jsonl(tmp_path / "n.jsonl", rows)
        pack_load.load_nodes("pack-1", f, builder, {})
        for i in range(4):
            _poke_doc(docs, f"n{i}", _UNREADABLE_ROWS["json5"])
        docs._conn.execute("UPDATE doc_nodes SET node_type='Stale' WHERE node_id='n4'")
        docs._conn.commit()
        assert _counts(_incremental(live, pack_sql, f)) == (0, 1, 4, 0, 0)

    def test_unreadable_row_keeps_same_branch_cleanup_and_vector_check(
            self, live, tmp_path, pack_sql):
        """same 경로의 구 space 정리와 벡터 미확인 집계는 종전대로 돈다."""
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, builder, {})
        _poke_doc(docs, "n1", _UNREADABLE_ROWS["json5"])
        docs.upsert_node_doc("concept", "Concept", "n1", {"pack_id": "pack-1"})
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        r = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, state["nodes"], graph, docs,
            state["doc_node_spaces"], vec=_RecordingVec(), sql=pack_sql)
        assert _counts(r) == (0, 0, 1, 0, 0)
        assert r[6] == 1, "벡터 미확인 집계가 종전대로 올라야 한다"
        assert _doc_row(docs, "concept", "n1") is None, "구 space 행이 정리되지 않았다"


class TestFingerprintScanFailure:
    def test_invalid_utf8_fails_the_scan_and_degrades_to_no_comparison(
            self, live, tmp_path, caplog, pack_sql):
        f, docs = _two_nodes(live, tmp_path, pack_sql)
        docs._conn.execute(
            "UPDATE doc_nodes SET properties=CAST(? AS TEXT) WHERE node_id='n1'",
            (b'{"pack_id":"pack-1","v":"\xff"}',))
        docs._conn.execute("UPDATE doc_nodes SET node_type='Stale' WHERE node_id='n2'")
        docs._conn.commit()
        with caplog.at_level(logging.WARNING):
            r = _incremental(live, pack_sql, f)
        assert _counts(r) == (0, 0, 2, 0, 0), "조회 실패 런은 문서 값을 비교하지 않는다"
        assert len(_warnings(caplog, "지문 조회 실패, 이번 런")) == 1
        assert len(_warnings(caplog, "해석 불가 비교 생략")) == 1

    def test_second_page_failure_returns_no_partial_map(
            self, live, tmp_path, monkeypatch, caplog, pack_sql):
        monkeypatch.setattr(pack_load, "_DOC_FP_BATCH", 2)
        builder, _g, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id=f"n{i}") for i in range(3)])
        pack_load.load_nodes("pack-1", f, builder, {})
        docs._conn.execute("UPDATE doc_nodes SET node_type='Stale' WHERE node_id='n0'")
        docs._conn.commit()
        real = docs._fetch_all

        def flaky(sql, params):
            if params.get("last_node_id"):
                raise RuntimeError("second page down")
            return real(sql, params)

        monkeypatch.setattr(docs, "_fetch_all", flaky)
        with caplog.at_level(logging.WARNING):
            r = _incremental(live, pack_sql, f)
        assert _counts(r) == (0, 0, 3, 0, 0)
        assert len(_warnings(caplog, "지문 조회 실패, 이번 런")) == 1

    def test_scan_failure_still_runs_same_branch_cleanup_and_vector_check(
            self, live, tmp_path, monkeypatch, pack_sql):
        builder, graph, docs = live
        f = _write_jsonl(tmp_path / "n.jsonl", [_node(id="n1")])
        pack_load.load_nodes("pack-1", f, builder, {})
        docs.upsert_node_doc("concept", "Concept", "n1", {"pack_id": "pack-1"})
        state = pack_load.live_pack_state("pack-1", graph, docs, _NoVec())
        real = docs._fetch_all

        def flaky(sql, params):
            if "last_node_id" in params:
                raise RuntimeError("scan down")
            return real(sql, params)

        monkeypatch.setattr(docs, "_fetch_all", flaky)
        r = pack_load.load_nodes_incremental(
            "pack-1", f, builder, {}, state["nodes"], graph, docs,
            state["doc_node_spaces"], vec=_RecordingVec(), sql=pack_sql)
        assert _counts(r) == (0, 0, 1, 0, 0)
        assert r[6] == 1
        assert _doc_row(docs, "concept", "n1") is None

    def test_file_side_fingerprint_failure_is_not_comparable(
            self, live, tmp_path, monkeypatch, caplog, pack_sql):
        f, docs = _two_nodes(live, tmp_path, pack_sql)
        real = pack_load._doc_content_fingerprint
        calls = {"n": 0}

        def flaky(node_type, props):
            calls["n"] += 1
            # 스캔(행 수만큼)이 끝난 뒤 첫 호출이 파일 쪽 호출이다
            if calls["n"] == 3:
                raise RuntimeError("file side down")
            return real(node_type, props)

        monkeypatch.setattr(pack_load, "_doc_content_fingerprint", flaky)
        with caplog.at_level(logging.WARNING):
            r = _incremental(live, pack_sql, f)
        assert _counts(r) == (0, 0, 2, 0, 0)
        msgs = _warnings(caplog, "해석 불가 비교 생략")
        assert len(msgs) == 1 and "1건" in msgs[0]
