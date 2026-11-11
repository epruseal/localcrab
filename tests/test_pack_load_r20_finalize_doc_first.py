"""r20 - 증분 마감은 문서 행을 먼저 지우고, 문서 행을 다 지운 노드만 그래프에서 지운다(#636).

`incremental_finalize` 는 예전에 입력에서 빠진 노드의 그래프 노드를 먼저 지우고, 그 뒤에
같은 팩의 문서 행을 지웠다. 문서 삭제 예외는 경고로 남기고 넘어갔다. 그래서 문서 삭제가
실패하거나 두 단계 사이에서 프로세스가 멈추면 그래프 노드 없이 문서 저장소에만 남은 행이
생겼다. 그 행은 노드 BM25 결과로 나오고 canonical 은 `node_not_found` 를 낸다.

이 파일은 두 시나리오로 고친 동작을 고정한다.

- 문서 삭제 실패를 주입한다. 그 노드의 그래프 노드는 남아야 하고, 다음 실행이 둘 다 지운다.
- 저장소 변경 호출의 모든 지점에서 중단을 주입한다. 어느 지점에서 멈춰도 문서 전용 행이
  없어야 하고, 같은 입력의 재실행이 남은 행을 전부 지운다.

두 시나리오 모두 입력에 남은 노드와 다른 팩의 행이 바뀌지 않는지(대조군)를 함께 본다.
저장소는 실 SQLite 그래프, 문서, 등록부와 파일 DB 위의 sqlite 벡터 테이블이다. 모든 판정은
파일 DB 에 별도 읽기 전용 연결을 열어 읽는다. 실패와 중단은 저장소 인스턴스 메서드를 감싸
주입한다.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.ontology.builder import OntologyBuilder
from opencrab.pack import load as pack_load
from opencrab.pack.ownership import create_pack
from opencrab.stores.local_graph_store import LocalGraphStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore
from opencrab.stores.sql_store import SQLStore
from tests._pack_fixtures import ensure_test_user
from tests.test_pack_load import (
    _LIVE_TEST_USER,
    _chunk,
    _node,
    _RecordingVec,
    _SqliteVecLike,
    _write_jsonl,
)

PACK = "pack-1"
OTHER = "pack-b"
KEEP_NODES = [f"n{i}" for i in range(10)]
STALE_NODES = ["s1", "s2"]
KEEP_CHUNKS = ["c1", "c2", "c3", "c4"]
STALE_CHUNK = "c5"
OTHER_NODES = ["m1", "m2"]
APPLIED_EDGES = {("n0", "cites", "n1")}
STALE_EDGES = {("n1", "cites", "n2"), ("s1", "cites", "n0")}
OTHER_EDGES = {("m1", "cites", "m2")}
KEEP_VECTORS = {"n0", "c1"}
STALE_VECTORS = {"s1", "s2", STALE_CHUNK}
OTHER_VECTORS = {"m1"}
# s1 은 문서 행을 두 공간에 갖는다. 적재가 쓴 공간과 시험이 더한 공간이다.
S1_EXTRA_SPACE = "evidence"

# 저장소 변경 메서드. 증분 마감이 행을 지울 때 부르는 것 전부다.
_MUTATORS = (
    ("graph", "delete_node"),
    ("graph", "delete_edge"),
    ("docs", "delete_node_doc"),
    ("docs", "delete_sources_with_state"),
    ("vec", "delete"),
)


class _Crash(BaseException):
    """프로세스 중단의 근사. 증분 마감의 `except Exception` 이 삼키지 못한다."""


class _FileVec(_SqliteVecLike):
    """`_SqliteVecLike` 와 같은 모양을 파일 DB 에 연다. 별도 연결로 읽기 위해서다."""

    def __init__(self, path: Path):
        self.available = True
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(
            f"CREATE TABLE {self._table} (node_id TEXT PRIMARY KEY, pack_id TEXT)")
        self._conn.commit()


def _edge(src, tgt):
    return {"id": f"{src}-{tgt}", "source_id": src, "target_id": tgt, "label": "CITES"}


def _rows(db: Path, sql: str) -> set[tuple]:
    """별도 읽기 전용 연결로 읽는다. 커밋되지 않은 삭제를 통과시키지 않는다."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return set(conn.execute(sql).fetchall())
    finally:
        conn.close()


class _Scenario:
    """한 디렉터리에 저장소 넷과 두 팩의 행을 만든다.

    pack-1 은 입력에 남는 노드 n0..n9 와 입력에서 빠진 s1, s2 를 갖는다. 엣지는 입력에 있는
    n0->n1, 입력에서 빠진 n1->n2, s1 의 인접 엣지 s1->n0 이다. 청크는 입력에 있는 c1..c4 와
    빠진 c5 다. pack-b 는 대조군이다. 삭제 후보 비율은 노드, 청크, 문서 모두 30% 아래라
    `force_delete=False` 로 핀을 지난다.
    """

    def __init__(self, root: Path):
        root.mkdir()
        self.root = root
        self.graph_db, self.doc_db, self.vec_db = (
            root / "graph.db", root / "doc.db", root / "vec.db")
        self.sql = SQLStore(f"sqlite:///{root / 'opencrab.db'}")
        ensure_test_user(self.sql, _LIVE_TEST_USER)
        for pack in (PACK, OTHER):
            create_pack(self.sql, _LIVE_TEST_USER, pack)
        self.graph = LocalGraphStore(str(self.graph_db))
        self.docs = LocalSQLDocStore(str(self.doc_db))
        self.vec = _FileVec(self.vec_db)
        builder = OntologyBuilder(self.graph, self.docs, self.sql)

        id_map: dict = {}
        nodes = _write_jsonl(root / "n.jsonl", [_node(id=i) for i in KEEP_NODES + STALE_NODES])
        pack_load.load_nodes(PACK, nodes, builder, id_map)
        edges = [_edge(f, t) for f, _r, t in sorted(APPLIED_EDGES | STALE_EDGES)]
        pack_load.load_edges(PACK, _write_jsonl(root / "e.jsonl", edges), builder, id_map)
        chunks = _write_jsonl(root / "c.jsonl", [_chunk(i) for i in range(1, 6)])
        pack_load.load_chunks(PACK, chunks, _RecordingVec(), self.docs, sql=self.sql)
        self.docs.upsert_node_doc(
            S1_EXTRA_SPACE, "Document", "s1", {"pack_id": PACK, "label": "s1 다른 공간"})

        other_nodes = _write_jsonl(root / "m.jsonl", [_node(id=i) for i in OTHER_NODES])
        pack_load.load_nodes(OTHER, other_nodes, builder, id_map)
        other_edges = [_edge(f, t) for f, _r, t in sorted(OTHER_EDGES)]
        pack_load.load_edges(OTHER, _write_jsonl(root / "me.jsonl", other_edges), builder, id_map)

        self.vec.seed(PACK, sorted(KEEP_VECTORS | STALE_VECTORS))
        self.vec.seed(OTHER, sorted(OTHER_VECTORS))

        state = pack_load.live_pack_state(PACK, self.graph, self.docs, self.vec)
        assert len(state["doc_node_spaces"]["s1"]) == 2, "전제: s1 의 문서 행이 두 공간에 있다"
        assert self.leftovers() == self.initial_leftovers(), "전제: 지울 행이 전부 있다"

    def close(self):
        self.graph.close()
        self.docs.close()
        self.vec._conn.close()

    def finalize(self):
        """같은 입력으로 증분 마감을 한 번 돈다. 라이브 상태는 매번 새로 읽는다."""
        state = pack_load.live_pack_state(PACK, self.graph, self.docs, self.vec)
        return pack_load.incremental_finalize(
            PACK, self.graph, self.docs, self.vec, state,
            set(KEEP_NODES), set(KEEP_CHUNKS), set(APPLIED_EDGES), False,
            len(KEEP_NODES), len(KEEP_CHUNKS), sql=self.sql)

    def graph_ids(self) -> set[str]:
        return {r[0] for r in _rows(self.graph_db, "SELECT node_id FROM graph_nodes")}

    def doc_keys(self) -> set[tuple[str, str]]:
        return set(_rows(self.doc_db, "SELECT space, node_id FROM doc_nodes"))

    def doc_ids(self) -> set[str]:
        return {node_id for _space, node_id in self.doc_keys()}

    def doc_only(self) -> set[str]:
        """그래프 노드 없이 문서 저장소에만 있는 node_id. 팩과 무관하게 전체를 본다."""
        return self.doc_ids() - self.graph_ids()

    def vector_rows(self) -> set[tuple]:
        return _rows(self.vec_db, f"SELECT node_id, pack_id FROM {self.vec._table}")

    def control(self) -> dict[str, set[tuple]]:
        """대조군 행 전체(모든 컬럼). 입력에 남은 pack-1 행과 pack-b 행이다."""
        node_ids = set(KEEP_NODES) | set(OTHER_NODES)
        return {
            "graph_nodes": {r for r in _rows(self.graph_db, "SELECT node_id, * FROM graph_nodes")
                            if r[0] in node_ids},
            "doc_nodes": {r for r in _rows(self.doc_db, "SELECT node_id, * FROM doc_nodes")
                          if r[0] in node_ids},
            "doc_sources": {r for r in _rows(self.doc_db, "SELECT source_id, * FROM doc_sources")
                            if r[0] in KEEP_CHUNKS},
            "graph_edges": {r for r in _rows(
                self.graph_db, "SELECT from_id, relation, to_id, * FROM graph_edges")
                if r[:3] in APPLIED_EDGES | OTHER_EDGES},
            "vectors": {r for r in self.vector_rows() if r[0] in KEEP_VECTORS | OTHER_VECTORS},
        }

    def leftovers(self) -> dict[str, set]:
        """지워야 할 행 가운데 아직 남은 것."""
        edges = set(_rows(self.graph_db, "SELECT from_id, relation, to_id FROM graph_edges"))
        sources = {r[0] for r in _rows(self.doc_db, "SELECT source_id FROM doc_sources")}
        return {
            "graph_nodes": self.graph_ids() & set(STALE_NODES),
            "doc_nodes": self.doc_ids() & set(STALE_NODES),
            "doc_sources": sources & {STALE_CHUNK},
            "graph_edges": edges & STALE_EDGES,
            "vectors": {r[0] for r in self.vector_rows()} & STALE_VECTORS,
        }

    @staticmethod
    def initial_leftovers() -> dict[str, set]:
        return {
            "graph_nodes": set(STALE_NODES),
            "doc_nodes": set(STALE_NODES),
            "doc_sources": {STALE_CHUNK},
            "graph_edges": set(STALE_EDGES),
            "vectors": set(STALE_VECTORS),
        }


_NOTHING_LEFT = {key: set() for key in _Scenario.initial_leftovers()}


class _MutationClock:
    """저장소 변경 호출을 순서대로 센다. `crash_at` 번째 호출 직전에 한 번만 중단한다.

    감싼 메서드는 원 메서드를 그대로 부르고 그 반환값과 예외를 그대로 넘긴다.
    """

    def __init__(self, scenario: _Scenario, crash_at: int | None = None):
        self.calls: list[str] = []
        self.crash_at = crash_at
        for attr, name in _MUTATORS:
            store = getattr(scenario, attr)
            real = getattr(store, name)

            def _wrapped(*args, _name=name, _real=real, **kwargs):
                if self.crash_at is not None and len(self.calls) + 1 == self.crash_at:
                    self.crash_at = None
                    raise _Crash(_name)
                self.calls.append(_name)
                return _real(*args, **kwargs)

            setattr(store, name, _wrapped)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """쓰기 가드가 요구하는 데이터 디렉터리와 등록된 principal 을 연다."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    principal = Principal(user_id=_LIVE_TEST_USER, is_local=True, disabled=False)
    with principal_scope(principal):
        yield tmp_path


@pytest.mark.parametrize("fail_on", [1, 2], ids=["first-space-fails", "second-space-fails"])
def test_doc_delete_failure_keeps_the_graph_node(env, caplog, fail_on):
    """받아들임 기준 1, 3. 문서 행 하나라도 못 지운 노드는 그래프에서 지우지 않는다.

    s1 의 두 공간 가운데 `fail_on` 번째 호출이 실패한다. 실패 집계는 노드 단위여야 한다.
    마지막 공간의 결과로 앞선 실패를 덮으면 첫 번째 공간 실패에서 문서 전용 행이 생긴다.
    """
    sc = _Scenario(env / "t1")
    try:
        before = sc.control()
        real = sc.docs.delete_node_doc
        s1_calls = []

        def _failing(space, node_id):
            if node_id == "s1":
                s1_calls.append(space)
                if len(s1_calls) == fail_on:
                    raise RuntimeError("주입한 문서 삭제 실패")
            return real(space, node_id)

        sc.docs.delete_node_doc = _failing
        with caplog.at_level(logging.WARNING, logger="opencrab.pack.load"):
            res = sc.finalize()

        failed_space = s1_calls[fail_on - 1]
        assert len(s1_calls) == 2, f"전제: s1 의 두 공간을 모두 시도한다: {s1_calls}"
        assert sc.doc_only() == set(), (
            f"문서 삭제가 실패한 노드의 그래프 노드를 지웠다: 문서 전용 {sc.doc_only()}")
        assert "s1" in sc.graph_ids(), "문서 행이 남은 노드의 그래프 노드를 지웠다"
        assert {k for k in sc.doc_keys() if k[1] == "s1"} == {(failed_space, "s1")}, (
            "실패한 공간의 문서 행만 남아야 한다")
        assert "s2" not in sc.graph_ids() and "s2" not in sc.doc_ids(), "다른 후보는 지워져야 한다"
        assert res["node_del"] == 1, res
        assert any("그래프 노드 삭제 보류" in r.getMessage() for r in caplog.records), (
            "보류한 그래프 삭제가 경고로 보이지 않는다")
        assert sc.control() == before, "대조군 행이 바뀌었다"

        sc.docs.delete_node_doc = real
        sc.finalize()
        assert sc.leftovers() == _NOTHING_LEFT, f"재실행이 남은 행을 다 지우지 못했다: {sc.leftovers()}"
        assert sc.doc_only() == set()
        assert sc.control() == before, "재실행이 대조군 행을 바꿨다"
    finally:
        sc.close()


def test_interruption_at_every_mutation_point_is_recovered_by_a_rerun(env):
    """받아들임 기준 2, 3. 변경 호출의 어느 지점에서 멈춰도 문서 전용 행이 없고, 재실행이 다 지운다."""
    probe = _Scenario(env / "count")
    try:
        clock = _MutationClock(probe)
        probe.finalize()
        sequence = list(clock.calls)
        assert probe.leftovers() == _NOTHING_LEFT, "전제: 중단 없는 실행이 다 지운다"
    finally:
        probe.close()
    assert sequence, "전제: 증분 마감이 저장소 변경 호출을 한다"

    partial_doc_seen = graph_without_doc_seen = False
    for k in range(1, len(sequence) + 1):
        sc = _Scenario(env / f"k{k}")
        try:
            before = sc.control()
            _MutationClock(sc, crash_at=k)
            with pytest.raises(_Crash):
                sc.finalize()
            where = f"k={k} ({sequence[k - 1]} 직전, 호출 순서 {sequence})"
            assert sc.doc_only() == set(), f"{where}: 문서 전용 행 {sc.doc_only()}"
            assert sc.control() == before, f"{where}: 중단이 대조군 행을 바꿨다"
            s1_docs = {k2 for k2 in sc.doc_keys() if k2[1] == "s1"}
            if "s1" in sc.graph_ids() and len(s1_docs) == 1:
                partial_doc_seen = True
            if any(n in sc.graph_ids() and n not in sc.doc_ids() for n in STALE_NODES):
                graph_without_doc_seen = True

            sc.finalize()   # 같은 입력, force_delete=False. 핀에 걸리면 SystemExit 로 실패한다.
            assert sc.leftovers() == _NOTHING_LEFT, f"{where}: 재실행 뒤 남은 행 {sc.leftovers()}"
            assert sc.doc_only() == set(), f"{where}: 재실행 뒤 문서 전용 행 {sc.doc_only()}"
            assert sc.control() == before, f"{where}: 재실행이 대조군 행을 바꿨다"
        finally:
            sc.close()

    assert partial_doc_seen, "순회가 s1 의 두 공간 사이 중단을 거치지 않았다"
    assert graph_without_doc_seen, "순회가 문서 행을 다 지운 뒤 그래프 삭제 전 중단을 거치지 않았다"
