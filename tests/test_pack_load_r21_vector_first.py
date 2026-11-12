"""r21 - 두 삭제 경로는 벡터를 소유 행보다 먼저 지운다(#640).

`incremental_finalize` 와 `delete_pack` 은 예전에 그래프 노드와 문서 원본(doc_sources)을 지운
뒤에 벡터를 지웠다. 그래서 두 단계 사이에서 멈추거나 벡터 삭제가 실패하면 소유 행 없는 벡터가
남았다. 하이브리드 질의의 벡터 레그는 소유 행을 확인하지 않고 그 행을 내고, canonical 은
`node_not_found` 다.

이 파일은 소유 행 없는 벡터가 생기지 않는지를 고정한다. 소유 행은 노드 벡터에는 그래프 노드,
청크 벡터에는 doc_sources 행이다. 판정 `ownerless()` 는 이 팩 벡터 id 가운데 그래프 노드 id 도
doc_sources id 도 아닌 집합이다.

- 증분 마감: 벡터 삭제 실패, 저장소 변경 호출 모든 지점의 중단, 확인할 수 없는 백엔드, 공유
  id, 열거 불가 백엔드(계약이 적은 불가피 조건), 백엔드 갈래 판정.
- 팩 삭제: 벡터 축 실패, 모든 지점의 중단, 미가용 백엔드, 삭제 뒤 생존(chroma).

모든 시험은 입력에 남은 행과 다른 팩의 행이 바뀌지 않는지(대조군)를 함께 본다. 저장소와 판정
방식은 r20(#636)과 같다. 실 SQLite 그래프, 문서, 등록부와 파일 DB 벡터 테이블을 쓰고 판정은 별도
읽기 전용 연결로 읽는다. 벡터 테이블 연결은 DELETE 문 직전에 실패나 중단을 주입하는 감싸개로
바꾼다.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from opencrab.auth import Principal, principal_scope
from opencrab.ontology.builder import OntologyBuilder
from opencrab.pack import delete_journal
from opencrab.pack import load as pack_load
from tests.test_pack_load import _LIVE_TEST_USER, _FakeChromaVec, _RecordingVec, _write_jsonl
from tests.test_pack_load_r20_finalize_doc_first import (
    APPLIED_EDGES,
    KEEP_CHUNKS,
    KEEP_NODES,
    OTHER,
    OTHER_NODES,
    PACK,
    STALE_CHUNK,
    STALE_NODES,
    _Crash,
    _rows,
    _Scenario,
)

SHARED = "x1"   # 노드 id 와 청크 id 가 같은 행(공유 id)
_NOTHING_LEFT = {"graph_nodes": set(), "doc_nodes": set(), "doc_sources": set(), "vectors": set()}


class _Conn:
    """sqlite3 연결을 감싼다. DELETE 문을 실행하기 직전에 `hook(params)` 을 부른다."""

    def __init__(self, conn):
        self._conn = conn
        self.hook = None

    def execute(self, sql, params=()):
        if self.hook is not None and sql.lstrip().upper().startswith("DELETE"):
            self.hook(list(params))
        return self._conn.execute(sql, params)

    def executemany(self, sql, seq):
        return self._conn.executemany(sql, seq)

    def commit(self):
        return self._conn.commit()

    def close(self):
        return self._conn.close()


def _fail_when(predicate):
    """DELETE 인자가 `predicate` 를 만족하면 예외를 던지는 감싸개 훅."""

    def _hook(params):
        if predicate(params):
            raise RuntimeError("주입한 벡터 삭제 실패")

    return _hook


class _Hidden:
    """가용하지만 `_vec_shape` 가 볼 속성이 없는 백엔드(계약의 열거 불가 조건).

    `delete(ids)` 는 같은 파일 DB 에서 id 로 지운다.
    """

    def __init__(self, inner):
        self.available = True
        self._inner = inner

    def delete(self, ids):
        self._inner.delete(ids)


class _ShapeHostile:
    """미가용이고 모양 판별이 예외로 끝나는 백엔드. 실 `SqliteVecStore` 가 확장 적재에
    실패했을 때 `_conn` property 가 판별 중에 예외를 다시 던지는 모양이다."""

    available = False

    @property
    def _conn(self):
        raise RuntimeError("shape probe failed")

    def delete(self, ids):
        raise RuntimeError("vector store unavailable")


class _AvailableFailsOnce:
    """첫 가용성 읽기는 실패하고 뒤 읽기는 거짓을 내는 더블이다.

    상태 열거 다음 마감 읽기에서 예외가 나는 갈래를 시험한다. 실 백엔드의 `available` 은
    단순 속성이므로 현재 그 예외를 낸다는 주장은 아니다.
    """

    def __init__(self):
        self.reads = 0
        self.delete_calls: list[list[str]] = []

    @property
    def available(self):
        self.reads += 1
        if self.reads == 1:
            raise RuntimeError("available read failed")
        return False

    def delete(self, ids):
        self.delete_calls.append(list(ids))
        raise AssertionError("delete must not be reached")


class _VecScenario(_Scenario):
    """r20 시나리오에 벡터 DELETE 주입 지점을 더한다. `shared=True` 면 공유 id 행을 더한다."""

    def __init__(self, root: Path, *, shared: bool = False):
        super().__init__(root)
        self.vec._conn = _Conn(self.vec._conn)
        if shared:
            builder = OntologyBuilder(self.graph, self.docs, self.sql)
            nodes = _write_jsonl(root / "x.jsonl", [
                {"id": SHARED, "label": "공유", "node_type": "Document", "space": "resource"}])
            pack_load.load_nodes(PACK, nodes, builder, {})
            chunks = _write_jsonl(root / "xc.jsonl", [
                {"id": SHARED, "document_id": "n1", "text": "공유 본문"}])
            pack_load.load_chunks(PACK, chunks, _RecordingVec(), self.docs, sql=self.sql)
            self.vec.seed(PACK, [SHARED])

    def finalize_with(self, nodes: set[str], chunks: set[str], *, vec=None):
        state = pack_load.live_pack_state(PACK, self.graph, self.docs, self.vec)
        return pack_load.incremental_finalize(
            PACK, self.graph, self.docs, self.vec if vec is None else vec, state,
            nodes, chunks, set(APPLIED_EDGES), False, len(nodes), len(chunks), sql=self.sql)

    def source_ids(self) -> set[str]:
        return {r[0] for r in _rows(self.doc_db, "SELECT source_id FROM doc_sources")}

    def vector_ids(self, pack: str = PACK) -> set[str]:
        return {node_id for node_id, owner in self.vector_rows() if owner == pack}

    def ownerless(self, ids: set[str] | None = None) -> set[str]:
        """소유 행 없는 벡터. 그래프 노드 id 도 doc_sources id 도 아닌 이 팩 벡터 id 다."""
        ids = self.vector_ids() if ids is None else ids
        return ids - self.graph_ids() - self.source_ids()

    def pack_rows(self) -> dict[str, set]:
        """팩 삭제가 지워야 할 PACK 행 가운데 남은 것."""
        pred = f"json_extract(properties, '$.pack_id') = '{PACK}'"
        return {
            "graph_nodes": {r[0] for r in _rows(
                self.graph_db, f"SELECT node_id FROM graph_nodes WHERE {pred}")},
            "doc_nodes": {r[0] for r in _rows(
                self.doc_db, f"SELECT node_id FROM doc_nodes WHERE {pred}")},
            "doc_sources": {r[0] for r in _rows(
                self.doc_db,
                "SELECT source_id FROM doc_sources "
                f"WHERE json_extract(metadata, '$.pack_id') = '{PACK}'")},
            "vectors": self.vector_ids(),
        }

    def other_rows(self) -> dict[str, set[tuple]]:
        """다른 팩(대조군)의 행 전체 컬럼."""
        return {
            "graph_nodes": {r for r in _rows(self.graph_db, "SELECT node_id, * FROM graph_nodes")
                            if r[0] in OTHER_NODES},
            "doc_nodes": {r for r in _rows(self.doc_db, "SELECT node_id, * FROM doc_nodes")
                          if r[0] in OTHER_NODES},
            "graph_edges": {r for r in _rows(
                self.graph_db, "SELECT from_id, relation, to_id, * FROM graph_edges")
                if r[0] in OTHER_NODES},
            "vectors": {r for r in self.vector_rows() if r[1] == OTHER},
        }

    def nothing_left_for_finalize(self) -> bool:
        return self.leftovers() == {k: set() for k in self.initial_leftovers()}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """쓰기 가드가 요구하는 데이터 디렉터리와 등록된 principal 을 연다."""
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    principal = Principal(user_id=_LIVE_TEST_USER, is_local=True, disabled=False)
    with principal_scope(principal):
        yield tmp_path


class _Clock:
    """저장소 변경 호출을 순서대로 센다. `crash_at` 번째 호출 직전에 한 번만 중단한다.

    감싼 메서드는 원 메서드를 그대로 부르고 그 반환값과 예외를 그대로 넘긴다. 벡터 DELETE 는
    연결 감싸개의 훅으로 센다. `mp` 를 주면 저널 저장도 센다.
    """

    def __init__(self, sc: _VecScenario, methods, *, mp=None, crash_at=None):
        self.calls: list[str] = []
        self.crash_at = crash_at
        for store, name in methods:
            obj = getattr(sc, store)
            real = getattr(obj, name)

            def _wrapped(*args, _name=f"{store}.{name}", _real=real, **kwargs):
                self._tick(_name)
                return _real(*args, **kwargs)

            setattr(obj, name, _wrapped)
        sc.vec._conn.hook = lambda _params: self._tick("vec.DELETE")
        if mp is not None:
            real_save = delete_journal.save_journal

            def _save(*args, **kwargs):
                self._tick("journal.save")
                return real_save(*args, **kwargs)

            mp.setattr(delete_journal, "save_journal", _save)

    def _tick(self, name):
        if self.crash_at is not None and len(self.calls) + 1 == self.crash_at:
            self.crash_at = None
            raise _Crash(name)
        self.calls.append(name)


_FINALIZE_MUTATORS = (
    ("graph", "delete_node"),
    ("graph", "delete_edge"),
    ("docs", "delete_node_doc"),
    ("docs", "delete_sources_with_state"),
)
_DELETE_MUTATORS = (
    ("graph", "delete_node"),
    ("docs", "delete_node_doc"),
    ("docs", "_exec_write"),
    ("docs", "delete_sources_with_state"),
)


def _warned(caplog, text: str) -> bool:
    return any(text in r.getMessage() for r in caplog.records)


# ───────────────────────── incremental_finalize ─────────────────────────


@pytest.mark.parametrize("fail", ["every-call", "orphan-batch"])
def test_finalize_vector_delete_failure_keeps_the_owner_rows(env, caplog, fail):
    """받아들임 기준 1, 2. 벡터 삭제가 실패한 id 의 그래프 노드와 원본은 이번 실행에서 남는다.

    `orphan-batch` 는 고아 정리 배치(s1, s2, c5)만 실패한다. 청크 벡터 호출(c5)은 성공하므로
    c5 의 원본은 벡터 없이 남는다. 그 상태는 소유 행 없는 벡터가 아니다.
    """
    sc = _VecScenario(env / "t")
    try:
        before = sc.control()
        if fail == "every-call":
            sc.vec._conn.hook = _fail_when(lambda _params: True)
        else:
            sc.vec._conn.hook = _fail_when(lambda params: "s1" in params)
        with caplog.at_level(logging.WARNING, logger="opencrab.pack.load"):
            res = sc.finalize()

        assert sc.ownerless() == set(), f"소유 행 없는 벡터가 생겼다: {sc.ownerless()}"
        assert set(STALE_NODES) <= sc.graph_ids(), "벡터를 못 지운 노드의 그래프 노드를 지웠다"
        assert {"s1", "s2"} <= sc.vector_ids(), "전제: 실패한 노드 벡터가 남는다"
        assert STALE_CHUNK in sc.source_ids(), "벡터를 못 지운 청크의 원본을 지웠다"
        assert res["node_del"] == 0 and res["chunk_del"] == 0, res
        assert _warned(caplog, "벡터 삭제 미확인 보류"), "보류가 경고로 보이지 않는다"
        assert sc.control() == before, "대조군 행이 바뀌었다"

        sc.vec._conn.hook = None
        sc.finalize()
        assert sc.nothing_left_for_finalize(), f"재실행이 남은 행을 다 지우지 못했다: {sc.leftovers()}"
        assert sc.ownerless() == set()
        assert sc.control() == before, "재실행이 대조군 행을 바꿨다"
    finally:
        sc.close()


def test_finalize_interruption_at_every_mutation_point_leaves_no_ownerless_vector(env):
    """받아들임 기준 1, 2. 어느 지점에서 멈춰도 소유 행 없는 벡터가 없고, 재실행이 다 지운다."""
    probe = _VecScenario(env / "count")
    try:
        clock = _Clock(probe, _FINALIZE_MUTATORS)
        probe.finalize()
        sequence = list(clock.calls)
        assert probe.nothing_left_for_finalize(), "전제: 중단 없는 실행이 다 지운다"
    finally:
        probe.close()
    assert "vec.DELETE" in sequence and "graph.delete_node" in sequence, sequence

    window_seen = False
    for k in range(1, len(sequence) + 1):
        sc = _VecScenario(env / f"k{k}")
        try:
            before = sc.control()
            _Clock(sc, _FINALIZE_MUTATORS, crash_at=k)
            with pytest.raises(_Crash):
                sc.finalize()
            where = f"k={k} ({sequence[k - 1]} 직전, 호출 순서 {sequence})"
            assert sc.ownerless() == set(), f"{where}: 소유 행 없는 벡터 {sc.ownerless()}"
            assert sc.control() == before, f"{where}: 중단이 대조군 행을 바꿨다"
            if any(n in sc.graph_ids() and n not in sc.vector_ids() for n in STALE_NODES):
                window_seen = True

            sc.vec._conn.hook = None
            sc.finalize()   # 같은 입력, force_delete=False. 핀에 걸리면 SystemExit 로 실패한다.
            assert sc.nothing_left_for_finalize(), f"{where}: 재실행 뒤 남은 행 {sc.leftovers()}"
            assert sc.ownerless() == set(), f"{where}: 재실행 뒤 소유 행 없는 벡터"
            assert sc.control() == before, f"{where}: 재실행이 대조군 행을 바꿨다"
        finally:
            sc.close()
    assert window_seen, "순회가 벡터 삭제 뒤 그래프 노드 삭제 전 지점을 거치지 않았다"


@pytest.mark.parametrize(
    "backend", ["shaped-unavailable", "shape-probe-fails", "available-read-fails"])
def test_finalize_unconfirmed_backend_defers_node_and_chunk_deletion(env, caplog, backend):
    """받아들임 기준 1, 2. 벡터 삭제를 확인할 수 없는 백엔드면 그래프 노드와 원본을 남긴다.

    앞의 두 사례에서는 `live_pack_state` 가 벡터 id 를 빈 집합으로 접는다. 세 번째 사례는
    정상 백엔드로 상태를 읽은 뒤 마감의 가용성 읽기에서 예외를 주입한다.
    """
    sc = _VecScenario(env / "t")
    try:
        before = sc.control()
        if backend == "shaped-unavailable":
            sc.vec.available = False
            vec = sc.vec
        elif backend == "shape-probe-fails":
            vec = _ShapeHostile()
        else:
            vec = _AvailableFailsOnce()
        state_reader = sc.vec if backend == "available-read-fails" else vec
        state = pack_load.live_pack_state(PACK, sc.graph, sc.docs, state_reader)
        if backend == "available-read-fails":
            assert {"s1", "s2", STALE_CHUNK} <= state["vec_ids"], "전제: 벡터 id 를 열거했다"
        else:
            assert state["vec_ids"] == set(), "전제: 벡터 id 를 열거하지 못한다"
        with caplog.at_level(logging.WARNING, logger="opencrab.pack.load"):
            res = pack_load.incremental_finalize(
                PACK, sc.graph, sc.docs, vec, state, set(KEEP_NODES), set(KEEP_CHUNKS),
                set(APPLIED_EDGES), False, len(KEEP_NODES), len(KEEP_CHUNKS), sql=sc.sql)

        assert sc.ownerless() == set(), f"소유 행 없는 벡터가 생겼다: {sc.ownerless()}"
        assert set(STALE_NODES) <= sc.graph_ids(), "확인할 수 없는 백엔드인데 그래프 노드를 지웠다"
        assert STALE_CHUNK in sc.source_ids(), "확인할 수 없는 백엔드인데 원본을 지웠다"
        assert res["node_del"] == 0 and res["chunk_del"] == 0 and res["vec_orphan_del"] == 0, res
        assert _warned(caplog, "벡터 백엔드 상태 미확인"), "사유가 경고로 보이지 않는다"
        if backend == "available-read-fails":
            assert _warned(caplog, "available-unknown"), "가용성 읽기 실패 사유가 보이지 않는다"
            assert vec.delete_calls == [], "가용성을 확인하지 못했는데 벡터 삭제를 불렀다"
        assert sc.control() == before

        sc.vec.available = True
        sc.finalize()
        assert sc.nothing_left_for_finalize(), f"가용 백엔드 재실행 뒤 남은 행 {sc.leftovers()}"
        assert sc.ownerless() == set()
        assert sc.control() == before
    finally:
        sc.close()


@pytest.mark.parametrize("case", ["node-only", "chunk-only", "both-with-failure"])
def test_finalize_shared_id_slot_follows_the_remaining_owner(env, case):
    """공유 id. 슬롯은 남은 소유 행을 따라 남고, 둘 다 빠지면 함께 지운다."""
    sc = _VecScenario(env / "t", shared=True)
    try:
        before = sc.control()
        nodes, chunks = set(KEEP_NODES) | set(STALE_NODES), set(KEEP_CHUNKS) | {STALE_CHUNK}
        if case == "node-only":
            sc.finalize_with(nodes, chunks | {SHARED})
            assert SHARED not in sc.graph_ids(), "입력에서 빠진 노드를 지우지 않았다"
            assert SHARED in sc.source_ids() and SHARED in sc.vector_ids(), (
                "입력에 남은 청크의 원본이나 슬롯을 지웠다")
        elif case == "chunk-only":
            sc.finalize_with(nodes | {SHARED}, chunks)
            assert SHARED not in sc.source_ids(), "입력에서 빠진 청크의 원본을 지우지 않았다"
            assert SHARED in sc.graph_ids() and SHARED in sc.vector_ids(), (
                "입력에 남은 노드의 그래프 노드나 슬롯을 지웠다")
        else:
            sc.vec._conn.hook = _fail_when(lambda params: SHARED in params)
            sc.finalize_with(nodes, chunks)
            assert {SHARED} <= sc.graph_ids() & sc.source_ids() & sc.vector_ids(), (
                "벡터를 못 지운 공유 id 의 그래프 노드나 원본을 지웠다")
            assert sc.ownerless() == set(), f"소유 행 없는 벡터가 생겼다: {sc.ownerless()}"
            sc.vec._conn.hook = None
            sc.finalize_with(nodes, chunks)
            assert SHARED not in sc.graph_ids() | sc.source_ids() | sc.vector_ids(), (
                "재실행이 공유 id 행을 다 지우지 못했다")
        assert sc.ownerless() == set(), f"소유 행 없는 벡터가 생겼다: {sc.ownerless()}"
        assert sc.control() == before
    finally:
        sc.close()


def test_finalize_non_enumerable_backend_keeps_the_documented_behaviour(env):
    """계약의 불가피 조건. 열거를 못 하는 가용 백엔드는 노드 벡터를 찾지 못하고 그래프는 지운다.

    청크 벡터는 id 로 지우므로 그 호출이 실패하면 원본을 남긴다.
    """
    sc = _VecScenario(env / "t")
    try:
        before = sc.control()
        hidden = _Hidden(sc.vec)
        state = pack_load.live_pack_state(PACK, sc.graph, sc.docs, hidden)
        assert state["vec_ids"] == set(), "전제: 열거를 못 한다"

        sc.vec._conn.hook = _fail_when(lambda _params: True)
        res = pack_load.incremental_finalize(
            PACK, sc.graph, sc.docs, hidden, state, set(KEEP_NODES), set(KEEP_CHUNKS),
            set(APPLIED_EDGES), False, len(KEEP_NODES), len(KEEP_CHUNKS), sql=sc.sql)
        assert res["node_del"] == len(STALE_NODES), "열거 불가 백엔드에서 그래프 삭제를 막았다"
        assert STALE_CHUNK in sc.source_ids(), "청크 벡터 삭제가 실패했는데 원본을 지웠다"
        assert {"s1", "s2"} <= sc.vector_ids(), "전제(불가피 조건): 노드 벡터는 남는다"
        assert sc.control() == before
    finally:
        sc.close()


class _NoShape:
    available = False


class _ShapedUnavailable:
    available = False
    _conn = None


@pytest.mark.parametrize(
    ("vec", "available", "mode"),
    [
        pytest.param(None, False, "absent", id="none"),
        pytest.param(_NoShape(), False, "absent", id="no-shape"),
        pytest.param(_ShapedUnavailable(), False, "unavailable", id="shaped-unavailable"),
        pytest.param(_NoShape(), None, "available-unknown", id="available-unknown"),
        pytest.param(_ShapeHostile(), False, "shape-unknown", id="shape-probe-fails"),
        pytest.param(_NoShape(), True, "ready", id="available"),
    ],
)
def test_vec_mode_branches(vec, available, mode):
    assert pack_load._vec_mode(vec, available) == mode


# ───────────────────────── delete_pack ─────────────────────────


def _delete(sc: _VecScenario, *, resume: bool = False, vec=None):
    return pack_load.delete_pack(
        PACK, sc.graph, sc.docs, sc.vec if vec is None else vec, sql=sc.sql, resume=resume)


def _axes(sc: _VecScenario) -> dict:
    return delete_journal.load_journal(sc.root, PACK)["axes"]


@pytest.mark.parametrize("backend", ["delete-fails", "shaped-unavailable"])
def test_delete_pack_unfinished_vector_axis_keeps_graph_nodes_and_sources(
        env, monkeypatch, caplog, backend):
    """받아들임 기준 1, 2. 벡터 축이 끝나지 않으면 그래프 노드와 doc_sources 를 지우지 않는다.

    doc_nodes 는 벡터의 소유 행이 아니므로 기다리지 않고 지운다.
    """
    sc = _VecScenario(env / "t")
    monkeypatch.setenv("LOCAL_DATA_DIR", str(sc.root))
    try:
        before_other = sc.other_rows()
        initial = sc.pack_rows()
        if backend == "delete-fails":
            sc.vec._conn.hook = _fail_when(lambda _params: True)
        else:
            sc.vec.available = False
        with caplog.at_level(logging.WARNING, logger="opencrab.pack.load"):
            _delete(sc)
        left = sc.pack_rows()
        assert sc.ownerless() == set(), f"소유 행 없는 벡터가 생겼다: {sc.ownerless()}"
        assert left["graph_nodes"] == initial["graph_nodes"], "벡터 축이 미완인데 그래프 노드를 지웠다"
        assert left["doc_sources"] == initial["doc_sources"], "벡터 축이 미완인데 원본을 지웠다"
        assert left["vectors"] == initial["vectors"]
        assert left["doc_nodes"] == set(), "doc_nodes 는 벡터를 기다리지 않고 지워야 한다"
        axes = _axes(sc)
        assert axes["vectors"]["done"] is False
        assert axes["doc_node_extra_and_sources"]["done"] is False
        assert axes["graph_nodes"].get("done") is not True
        assert _warned(caplog, "벡터 축 미완"), "보류가 경고로 보이지 않는다"
        assert sc.other_rows() == before_other, "다른 팩 행이 바뀌었다"

        sc.vec._conn.hook = None
        sc.vec.available = True
        _delete(sc, resume=True)
        assert sc.pack_rows() == _NOTHING_LEFT, f"재개가 남은 행을 다 지우지 못했다: {sc.pack_rows()}"
        assert sc.other_rows() == before_other
    finally:
        sc.close()


def test_delete_pack_interruption_at_every_point_leaves_no_ownerless_vector(env, monkeypatch):
    """받아들임 기준 1, 2. 어느 지점에서 멈춰도 소유 행 없는 벡터가 없고, 재개가 다 지운다."""
    probe = _VecScenario(env / "count")
    monkeypatch.setenv("LOCAL_DATA_DIR", str(probe.root))
    try:
        with monkeypatch.context() as mp:
            clock = _Clock(probe, _DELETE_MUTATORS, mp=mp)
            _delete(probe)
            sequence = list(clock.calls)
        assert probe.pack_rows() == _NOTHING_LEFT, "전제: 중단 없는 실행이 다 지운다"
    finally:
        probe.close()
    assert "vec.DELETE" in sequence and "graph.delete_node" in sequence, sequence

    window_seen = False
    for k in range(1, len(sequence) + 1):
        sc = _VecScenario(env / f"k{k}")
        monkeypatch.setenv("LOCAL_DATA_DIR", str(sc.root))
        try:
            before_other = sc.other_rows()
            with monkeypatch.context() as mp:
                _Clock(sc, _DELETE_MUTATORS, mp=mp, crash_at=k)
                with pytest.raises(_Crash):
                    _delete(sc)
            where = f"k={k} ({sequence[k - 1]} 직전, 호출 순서 {sequence})"
            assert sc.ownerless() == set(), f"{where}: 소유 행 없는 벡터 {sc.ownerless()}"
            assert sc.other_rows() == before_other, f"{where}: 다른 팩 행이 바뀌었다"
            left = sc.pack_rows()
            if left["graph_nodes"] and not left["vectors"]:
                window_seen = True

            sc.vec._conn.hook = None
            _delete(sc, resume=True)
            assert sc.pack_rows() == _NOTHING_LEFT, f"{where}: 재개 뒤 남은 행 {sc.pack_rows()}"
            assert sc.other_rows() == before_other, f"{where}: 재개가 다른 팩 행을 바꿨다"
        finally:
            sc.close()
    assert window_seen, "순회가 벡터 삭제 뒤 그래프 노드 삭제 전 지점을 거치지 않았다"


def test_delete_pack_chroma_survivor_keeps_the_vector_axis_open(env, monkeypatch, caplog):
    """받아들임 기준 1, 2. 삭제 뒤 재조회에서 생존이 확인되면 벡터 축은 done 이 아니다.

    chroma 의 delete 는 예외 없이 일부를 남길 수 있다. 그 축을 done 으로 두면 그래프 노드와
    doc_sources 축이 진행해 소유 행 없는 벡터가 남는다.
    """
    sc = _VecScenario(env / "t")
    monkeypatch.setenv("LOCAL_DATA_DIR", str(sc.root))
    try:
        before_other = sc.other_rows()
        initial = sc.pack_rows()
        chroma = _FakeChromaVec({**{i: PACK for i in initial["vectors"]},
                                 **{i: OTHER for i, _p in before_other["vectors"]}})
        chroma._collection.lossy_delete_ids = {"s1"}

        def chroma_ids(pack):
            return {i for i, owner in chroma._collection._rows.items() if owner == pack}

        with caplog.at_level(logging.WARNING, logger="opencrab.pack.load"):
            _delete(sc, vec=chroma)
        assert chroma_ids(PACK) == {"s1"}, "전제: s1 만 삭제 뒤에도 남는다"
        assert sc.ownerless(chroma_ids(PACK)) == set(), "소유 행 없는 벡터가 생겼다"
        left = sc.pack_rows()
        assert left["graph_nodes"] == initial["graph_nodes"], "생존 벡터가 있는데 그래프 노드를 지웠다"
        assert left["doc_sources"] == initial["doc_sources"], "생존 벡터가 있는데 원본을 지웠다"
        assert _axes(sc)["vectors"]["done"] is False, "생존 벡터가 있는데 벡터 축이 done 이다"
        assert _warned(caplog, "벡터 삭제 뒤 생존"), "생존이 경고로 보이지 않는다"

        chroma._collection.lossy_delete_ids = set()
        _delete(sc, resume=True, vec=chroma)
        assert chroma_ids(PACK) == set()
        assert {k: v for k, v in sc.pack_rows().items() if k != "vectors"} == {
            k: set() for k in ("graph_nodes", "doc_nodes", "doc_sources")}
        assert chroma_ids(OTHER) == {i for i, _p in before_other["vectors"]}
        assert sc.other_rows() == before_other
    finally:
        sc.close()
