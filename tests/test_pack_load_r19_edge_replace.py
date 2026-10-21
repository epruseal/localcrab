"""r19 - 엣지 속성 교체: 같은 키, 다른 속성인 기존 엣지를 CAS 로 교체한다.

`load_edges(..., replace_changed=True)` 는 팩 소유 엣지의 키(from_id, relation,
to_id)가 같고 속성만 다른 행을 `OntologyBuilder.add_edge(..., replace_own=True)`
로 보낸다. 교체는 `upsert_edge` 가 `EdgeIdentityConflict` 를 낸 뒤에만 일어나고,
`update_edge` 가 현재 digest 와 소유 팩을 한 트랜잭션 안에서 다시 확인한다.

픽스처는 `tests/test_pack_load.py` 의 `live` 와 `pack_sql` 을 재사용한다.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

import pytest

from opencrab.auth import Principal, create_user, principal_scope
from opencrab.common.graph_identity import EdgeIdentityConflict, canonical_edge_digest
from opencrab.pack import load as pack_load
from tests.test_pack_load import (  # noqa: F401 - 기존 픽스처 재사용
    _node,
    _NoVec,
    _write_jsonl,
    live,
    pack_sql,
)

_GRAPH_METHODS = (
    "lookup_node_type", "get_node", "get_edge", "upsert_edge",
    "get_edge_digest", "update_edge",
)
_KEY = ("n1", "cites", "n2")


class _Recorder:
    """그래프 스토어 메서드 호출과 예외를 기록하고 원 메서드에 위임한다."""

    def __init__(self, monkeypatch, graph):
        self.calls: list[str] = []
        self.raised: list[tuple[str, str]] = []
        for name in _GRAPH_METHODS:
            real = getattr(graph, name)

            def _wrapped(*args, _n=name, _r=real, **kwargs):
                self.calls.append(_n)
                try:
                    return _r(*args, **kwargs)
                except Exception as exc:
                    self.raised.append((_n, type(exc).__name__))
                    raise

            monkeypatch.setattr(graph, name, _wrapped)

    def count(self, name: str) -> int:
        return self.calls.count(name)


def _edge(src="n1", tgt="n2", label="CITES", **props):
    return {"id": "e", "source_id": src, "target_id": tgt, "label": label,
            "properties": props}


def _setup(live, tmp_path, pack="pack-a", node_ids=("n1", "n2")):
    builder, graph, docs = live
    nf = _write_jsonl(tmp_path / "nodes.jsonl", [_node(id=i) for i in node_ids])
    id_map: dict = {}
    pack_load.load_nodes(pack, nf, builder, id_map)
    return builder, graph, docs, id_map


def _edges_file(tmp_path, name, rows):
    return _write_jsonl(tmp_path / name, rows)


def _stored_edges(tmp_path) -> dict[tuple, dict]:
    conn = sqlite3.connect(f"file:{tmp_path / 'graph.db'}?mode=ro", uri=True)
    try:
        return {
            (r[0], r[1], r[2]): json.loads(r[3])
            for r in conn.execute(
                "SELECT from_id, relation, to_id, properties FROM graph_edges")
        }
    finally:
        conn.close()


def _load(pack, path, builder, id_map, **kw):
    return pack_load.load_edges(pack, path, builder, id_map, **kw)


def _intruder(pack_sql):
    uid = create_user(pack_sql, "edge-replace-intruder", is_local=False)
    return Principal(user_id=uid, is_local=False, disabled=False)


class TestReplaceChangedEdge:
    def test_changed_own_edge_is_replaced_by_cas(self, live, tmp_path):
        builder, graph, docs, id_map = _setup(live, tmp_path)
        f1 = _edges_file(tmp_path, "e1.jsonl", [_edge(note="a")])
        assert _load("pack-a", f1, builder, id_map, replace_changed=True) == (1, 0, 0)

        f2 = _edges_file(tmp_path, "e2.jsonl", [_edge(note="b")])
        counters: Counter = Counter()
        applied: set = set()
        res = _load("pack-a", f2, builder, id_map, applied=applied,
                    replace_changed=True, counters=counters)

        assert res == (1, 0, 0)
        assert counters["replaced"] == 1
        stored = _stored_edges(tmp_path)
        assert list(stored) == [_KEY], "엣지가 정확히 한 행이어야 한다"
        assert stored[_KEY]["note"] == "b"
        assert stored[_KEY]["pack_id"] == "pack-a"
        assert applied == {_KEY}

    def test_replacement_with_a_failed_leg_counts_as_error_not_replaced(
            self, live, tmp_path, monkeypatch):
        builder, graph, docs, id_map = _setup(live, tmp_path)
        f1 = _edges_file(tmp_path, "e1.jsonl", [_edge(note="a")])
        _load("pack-a", f1, builder, id_map, replace_changed=True)

        def _boom(*args, **kwargs):
            raise RuntimeError("registry write failed")

        monkeypatch.setattr(builder._sql, "register_edge", _boom)
        counters: Counter = Counter()
        f2 = _edges_file(tmp_path, "e2.jsonl", [_edge(note="b")])
        res = _load("pack-a", f2, builder, id_map, replace_changed=True, counters=counters)

        assert res == (0, 0, 1)
        assert counters["replaced"] == 0
        assert _stored_edges(tmp_path)[_KEY]["note"] == "b", "그래프 쓰기는 이미 반영됐다"

    def test_unchanged_rerun_takes_the_plain_upsert_path(self, live, tmp_path, monkeypatch):
        builder, graph, docs, id_map = _setup(live, tmp_path)
        f1 = _edges_file(tmp_path, "e1.jsonl", [_edge(note="a")])
        _load("pack-a", f1, builder, id_map, replace_changed=True)

        rec = _Recorder(monkeypatch, graph)
        counters: Counter = Counter()
        res = _load("pack-a", f1, builder, id_map, replace_changed=True, counters=counters)

        assert res == (1, 0, 0)
        assert counters["replaced"] == 0
        assert rec.count("upsert_edge") == 1
        assert rec.count("get_edge_digest") == 0, "변경 없는 행이 digest 를 미리 읽었다"
        assert rec.count("update_edge") == 0

    def test_default_keeps_the_old_conflict_behavior(self, live, tmp_path):
        builder, graph, docs, id_map = _setup(live, tmp_path)
        f1 = _edges_file(tmp_path, "e1.jsonl", [_edge(note="a")])
        _load("pack-a", f1, builder, id_map)
        f2 = _edges_file(tmp_path, "e2.jsonl", [_edge(note="b")])

        reasons: Counter = Counter()
        assert _load("pack-a", f2, builder, id_map, reasons=reasons) == (0, 0, 1)
        assert _stored_edges(tmp_path)[_KEY]["note"] == "a"
        assert any("EdgeIdentityConflict" in key[1] for key in reasons)

        with pytest.raises(EdgeIdentityConflict):
            builder.add_edge("resource", "n1", "cites", "resource", "n2",
                             properties={"note": "c"}, pack_id="pack-a", origin="server")

    def test_replace_own_is_refused_for_client_origin_and_fork_copy(
            self, live, tmp_path, monkeypatch):
        builder, graph, docs, id_map = _setup(live, tmp_path)
        rec = _Recorder(monkeypatch, graph)
        with pytest.raises(ValueError, match="replace_own"):
            builder.add_edge("resource", "n1", "cites", "resource", "n2",
                             pack_id="pack-a", replace_own=True)
        with pytest.raises(ValueError, match="replace_own"):
            builder.add_edge("resource", "n1", "cites", "resource", "n2",
                             pack_id="pack-a", origin="server",
                             fork_copy=True, replace_own=True)
        assert rec.calls == []


class TestRefusals:
    def test_foreign_pack_edge_is_never_replaced(self, live, tmp_path, monkeypatch):
        builder, graph, docs, id_map = _setup(live, tmp_path)
        graph.upsert_edge("Document", "n1", "cites", "Document", "n2",
                          {"pack_id": "pack-b", "note": "x"})
        before = _stored_edges(tmp_path)

        rec = _Recorder(monkeypatch, graph)
        counters: Counter = Counter()
        f = _edges_file(tmp_path, "e.jsonl", [_edge(note="y")])
        res = _load("pack-a", f, builder, id_map, replace_changed=True, counters=counters)

        assert res == (0, 1, 0)
        assert counters["replaced"] == 0
        assert rec.count("upsert_edge") == 0
        assert rec.count("get_edge_digest") == 0
        assert rec.count("update_edge") == 0
        assert _stored_edges(tmp_path) == before

    def test_non_owner_principal_touches_no_graph_method(
            self, live, tmp_path, monkeypatch, pack_sql):
        builder, graph, docs, id_map = _setup(live, tmp_path)
        f1 = _edges_file(tmp_path, "e1.jsonl", [_edge(note="a")])
        _load("pack-a", f1, builder, id_map)
        f2 = _edges_file(tmp_path, "e2.jsonl", [_edge(note="b")])

        rec = _Recorder(monkeypatch, graph)
        counters: Counter = Counter()
        with principal_scope(_intruder(pack_sql)):
            res = _load("pack-a", f2, builder, id_map, replace_changed=True,
                        counters=counters)
            with pytest.raises(Exception, match="pack-a"):
                builder.add_edge("resource", "n1", "cites", "resource", "n2",
                                 properties={"note": "b"}, pack_id="pack-a",
                                 origin="server", replace_own=True)

        assert res == (0, 0, 1)
        assert counters["replaced"] == 0
        assert rec.calls == [], f"비소유 호출이 그래프에 닿았다: {rec.calls}"
        assert _stored_edges(tmp_path)[_KEY]["note"] == "a"

        # 대조군: 소유자는 같은 파일로 교체한다.
        res = _load("pack-a", f2, builder, id_map, replace_changed=True, counters=counters)
        assert res == (1, 0, 0) and counters["replaced"] == 1
        assert _stored_edges(tmp_path)[_KEY]["note"] == "b"

    def test_unbound_principal_touches_no_graph_method(self, live, tmp_path, monkeypatch):
        builder, graph, docs, id_map = _setup(live, tmp_path)
        f1 = _edges_file(tmp_path, "e1.jsonl", [_edge(note="a")])
        _load("pack-a", f1, builder, id_map)
        f2 = _edges_file(tmp_path, "e2.jsonl", [_edge(note="b")])

        rec = _Recorder(monkeypatch, graph)

        def _unbound():
            raise LookupError

        monkeypatch.setattr("opencrab.auth.current_principal", _unbound)
        with pytest.raises(LookupError):
            builder.add_edge("resource", "n1", "cites", "resource", "n2",
                             properties={"note": "b"}, pack_id="pack-a",
                             origin="server", replace_own=True)
        with pytest.raises(RuntimeError, match="principal_scope"):
            _load("pack-a", f2, builder, id_map, replace_changed=True)

        assert rec.calls == []
        assert _stored_edges(tmp_path)[_KEY]["note"] == "a"

    def test_legacy_edge_without_pack_id_is_refused_by_update_edge(
            self, live, tmp_path, monkeypatch):
        builder, graph, docs, id_map = _setup(live, tmp_path)
        graph.upsert_edge("Document", "n1", "cites", "Document", "n2", {"note": "x"})
        before = _stored_edges(tmp_path)

        rec = _Recorder(monkeypatch, graph)
        reasons: Counter = Counter()
        counters: Counter = Counter()
        f = _edges_file(tmp_path, "e.jsonl", [_edge(note="y")])
        res = _load("pack-a", f, builder, id_map, reasons=reasons,
                    replace_changed=True, counters=counters)

        assert res == (0, 0, 1)
        assert counters["replaced"] == 0
        assert rec.count("update_edge") == 1
        assert ("update_edge", "EdgeIdentityConflict") in rec.raised
        assert _stored_edges(tmp_path) == before

    def test_stale_digest_fails_the_row_and_keeps_the_key_protected(
            self, live, tmp_path, monkeypatch, pack_sql):
        builder, graph, docs, id_map = _setup(live, tmp_path, node_ids=("n1", "n2", "n3"))
        f1 = _edges_file(tmp_path, "e1.jsonl",
                         [_edge(note="a"), _edge(tgt="n3", note="keep-or-drop")])
        assert _load("pack-a", f1, builder, id_map, replace_changed=True) == (2, 0, 0)
        state = pack_load.live_pack_state("pack-a", graph, docs, _NoVec())
        assert len(state["edges"]) == 2, "전제 위반"

        stale = canonical_edge_digest(
            "n1", "cites", "n2", "Document", "Document",
            {"pack_id": "pack-a", "note": "someone-else"})
        monkeypatch.setattr(graph, "get_edge_digest", lambda *a, **kw: stale)
        rec = _Recorder(monkeypatch, graph)

        reasons: Counter = Counter()
        counters: Counter = Counter()
        applied: set = set()
        f2 = _edges_file(tmp_path, "e2.jsonl", [_edge(note="b")])
        res = _load("pack-a", f2, builder, id_map, applied=applied, reasons=reasons,
                    replace_changed=True, counters=counters)

        assert res == (0, 0, 1)
        assert counters["replaced"] == 0
        assert ("update_edge", "EdgeIdentityConflict") in rec.raised
        assert any("EdgeIdentityConflict" in key[1] for key in reasons)
        assert _stored_edges(tmp_path)[_KEY]["note"] == "a"
        assert applied == {_KEY}

        fin = pack_load.incremental_finalize(
            "pack-a", graph, docs, _NoVec(), state, {"n1", "n2", "n3"}, set(),
            applied, True, 3, 0, sql=pack_sql)
        stored = _stored_edges(tmp_path)
        assert fin["edge_del"] == 1, "삭제 대조군: applied 밖의 엣지는 지워져야 한다"
        assert _KEY in stored, "실패한 교체 행의 키가 stale 로 지워졌다"
        assert ("n1", "cites", "n3") not in stored


class TestSameFinalKeyInOneFile:
    def test_second_row_with_the_same_key_stays_a_visible_error(self, live, tmp_path):
        builder, graph, docs, id_map = _setup(live, tmp_path)
        rows = [_edge(label="CITES"), _edge(label="CONTAINS")]
        f = _edges_file(tmp_path, "e.jsonl", rows)

        for run in (1, 2):
            reasons: Counter = Counter()
            counters: Counter = Counter()
            res = _load("pack-a", f, builder, id_map, reasons=reasons,
                        replace_changed=True, counters=counters)
            assert res == (1, 0, 1), f"run {run}: {res}"
            assert counters["replaced"] == 0, f"run {run}: 두 행이 서로를 교체했다"
            assert any("EdgeIdentityConflict" in key[1] for key in reasons)
            assert _stored_edges(tmp_path)[_KEY]["source_label"] == "CITES"


class TestOneCycleWithNodeChange:
    def test_node_type_change_and_edge_change_converge_in_one_run(
            self, live, tmp_path, pack_sql):
        builder, graph, docs, id_map = _setup(live, tmp_path)
        f1 = _edges_file(tmp_path, "e1.jsonl", [_edge(note="a")])
        _load("pack-a", f1, builder, id_map, replace_changed=True)

        nf2 = _write_jsonl(tmp_path / "nodes2.jsonl",
                           [_node(id="n1", node_type="File"), _node(id="n2")])
        f2 = _edges_file(tmp_path, "e2.jsonl", [_edge(note="b")])

        def run_cycle():
            state = pack_load.live_pack_state("pack-a", graph, docs, _NoVec())
            node_res = pack_load.load_nodes_incremental(
                "pack-a", nf2, builder, {}, state["nodes"], graph, docs,
                state["doc_node_spaces"], sql=pack_sql)
            counters: Counter = Counter()
            edge_res = _load("pack-a", f2, builder, id_map, replace_changed=True,
                             counters=counters)
            return node_res, edge_res, counters

        node_res, edge_res, counters = run_cycle()
        assert (node_res[0], node_res[1], node_res[4]) == (0, 1, 0), node_res[:5]
        assert edge_res == (1, 0, 0) and counters["replaced"] == 1
        stored = _stored_edges(tmp_path)
        assert stored[_KEY]["note"] == "b"
        conn = sqlite3.connect(f"file:{tmp_path / 'graph.db'}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT from_type, to_type FROM graph_edges").fetchone()
        finally:
            conn.close()
        assert tuple(row) == ("File", "Document")

        node_res, edge_res, counters = run_cycle()
        assert node_res[2] == 2, "두 번째 실행에서 노드가 전부 same 이어야 한다"
        assert edge_res == (1, 0, 0) and counters["replaced"] == 0
