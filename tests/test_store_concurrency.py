"""
스토어 in-process 동시성 테스트 — 멀티스레드 안전성 검증.

작업 전 작성(정상/에러/엣지) → 작업 후 검증. 모든 테스트는 pytest tmp_path 만 사용하므로
실데이터(/home/asdf/.openclaw/workspace/data/localcrab)는 절대 건드리지 않는다.

대상:
  - LocalGraphStore : 단일 sqlite3.Connection 공유 + threading.Lock(쓰기 직렬화)
  - LocalSQLDocStore: 기존 lock+WAL — 동시 읽기/쓰기 혼합 회귀
  - LocalDocStore   : JSON 파일 — 쓰기 직렬화 + atomic read
  - ChromaStore     : Chroma 자체 스레드 안전 + reset_collection 핸들 보호
  - 다중 스토어 fan-out : graph+doc 동시 쓰기 데드락 부재(로더/builder 패턴 대리)
"""

from __future__ import annotations

import threading
from typing import Any, Callable

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_threads(
    target: Callable[[int], None], n_threads: int, timeout: float = 60.0
) -> list[Exception]:
    """target(tid) 를 n_threads 개 스레드로 동시 실행. 예외를 모아 반환하고,
    timeout 내 종료하지 못한 스레드가 있으면(데드락 의심) 즉시 실패시킨다."""
    errors: list[Exception] = []
    lock = threading.Lock()

    def wrap(tid: int) -> None:
        try:
            target(tid)
        except Exception as exc:  # noqa: BLE001 - 테스트가 모든 예외를 수집
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=wrap, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout)
    alive = [th for th in threads if th.is_alive()]
    assert not alive, f"{len(alive)}개 스레드가 종료되지 않음 (데드락 의심)"
    return errors


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_store(tmp_path):
    from opencrab.stores.local_graph_store import LocalGraphStore

    s = LocalGraphStore(str(tmp_path / "graph.db"))
    assert s.available
    yield s
    s.close()


@pytest.fixture
def sql_doc_store(tmp_path):
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    s = LocalSQLDocStore(str(tmp_path / "doc_store.db"))
    assert s.available
    yield s
    s.close()


@pytest.fixture
def json_doc_store(tmp_path):
    from opencrab.stores.local_doc_store import LocalDocStore

    return LocalDocStore(str(tmp_path / "docs"))


@pytest.fixture
def chroma_store(tmp_path):
    pytest.importorskip("chromadb")
    from opencrab.stores.chroma_store import ChromaStore

    s = ChromaStore(
        host="localhost",
        port=8000,
        collection_name="test_concurrency",
        local_mode=True,
        local_path=str(tmp_path / "chroma"),
    )
    if not s.available:
        pytest.skip("ChromaDB가 이 환경에서 초기화되지 않음(임베딩 모델 미가용 등)")
    return s


@pytest.fixture
def sqlite_vec_store(tmp_path):
    pytest.importorskip("sqlite_vec")
    from _vec_helpers import MockEF
    from opencrab.stores.sqlite_vec_store import SqliteVecStore

    s = SqliteVecStore(
        db_path=str(tmp_path / "vectors.db"),
        embedding_function=MockEF(16),
        dim=16,
        collection_name="test_concurrency",
    )
    if not s.available:
        pytest.skip("sqlite-vec가 이 환경에서 초기화되지 않음")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# LocalGraphStore (핵심) — threading.Lock 쓰기 직렬화
# ---------------------------------------------------------------------------


class TestLocalGraphStoreConcurrency:
    def test_concurrent_node_writes(self, graph_store):
        """정상: 10스레드 × 50 upsert_node → 에러 0, 총 500개."""
        N, M = 10, 50

        def worker(tid: int) -> None:
            for i in range(M):
                graph_store.upsert_node("T", f"t{tid}_n{i}", {"tid": tid, "i": i})

        errors = run_threads(worker, N)
        assert errors == [], f"동시 쓰기 에러: {errors}"
        assert graph_store.count_nodes("T") == N * M

    def test_concurrent_node_and_edge_writes(self, graph_store):
        """정상: 동시 노드+엣지 쓰기(엔드포인트 자가 생성) → 에러 0, 카운트 일치."""
        N, M = 8, 40

        def worker(tid: int) -> None:
            for i in range(M):
                a, b = f"e{tid}_{i}_a", f"e{tid}_{i}_b"
                graph_store.upsert_node("T", a, {})
                graph_store.upsert_node("T", b, {})
                graph_store.upsert_edge("T", a, "REL", "T", b, {"tid": tid})

        errors = run_threads(worker, N)
        assert errors == [], f"동시 노드/엣지 쓰기 에러: {errors}"
        assert graph_store.count_nodes("T") == N * M * 2
        assert len(graph_store.export_edges()) == N * M

    def test_concurrent_read_write_mixed(self, graph_store):
        """정상: 쓰기 스레드와 읽기 스레드 혼합 → 예외 없음."""
        # 시드 노드(읽기 대상)
        for i in range(20):
            graph_store.upsert_node("T", f"seed{i}", {"i": i})
            if i:
                graph_store.upsert_edge("T", f"seed{i-1}", "NEXT", "T", f"seed{i}", {})

        stop = threading.Event()

        def writer(tid: int) -> None:
            for i in range(100):
                graph_store.upsert_node("T", f"w{tid}_{i}", {"tid": tid})
            stop.set()

        def reader(tid: int) -> None:
            # 쓰기가 끝날 때까지 계속 읽는다.
            while not stop.is_set():
                graph_store.get_node("T", "seed0")
                graph_store.count_nodes("T")
                graph_store.find_neighbors("seed0", depth=2, limit=10)

        errors: list[Exception] = []
        lk = threading.Lock()

        def run(fn, tid):
            try:
                fn(tid)
            except Exception as exc:  # noqa: BLE001
                with lk:
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(writer, t)) for t in range(3)]
        threads += [threading.Thread(target=run, args=(reader, t)) for t in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60.0)
        assert not [th for th in threads if th.is_alive()], "데드락 의심"
        assert errors == [], f"혼합 읽기/쓰기 에러: {errors}"

    def test_concurrent_single_and_batch(self, graph_store):
        """엣지: 단건 upsert_node 와 upsert_nodes_batch 동시 실행."""
        N = 6

        def worker(tid: int) -> None:
            if tid % 2 == 0:
                for i in range(50):
                    graph_store.upsert_node("T", f"s{tid}_{i}", {})
            else:
                batch = [
                    {"node_type": "T", "node_id": f"b{tid}_{i}", "properties": {"i": i}}
                    for i in range(50)
                ]
                graph_store.upsert_nodes_batch(batch)

        errors = run_threads(worker, N)
        assert errors == [], f"단건/배치 동시 에러: {errors}"
        assert graph_store.count_nodes("T") == N * 50

    def test_concurrent_same_key_upsert(self, graph_store):
        """엣지: 동일 키를 여러 스레드가 동시 upsert → last-write-wins, 1개만 존재."""

        def worker(tid: int) -> None:
            for _ in range(100):
                graph_store.upsert_node("T", "shared", {"tid": tid})

        errors = run_threads(worker, 10)
        assert errors == [], f"동일 키 동시 upsert 에러: {errors}"
        assert graph_store.count_nodes("T") == 1
        assert graph_store.get_node("T", "shared") is not None

    def test_concurrent_delete_and_upsert(self, graph_store):
        """엣지: delete_node 와 upsert 경합 → 에러 없음."""
        for i in range(100):
            graph_store.upsert_node("T", f"d{i}", {})

        def worker(tid: int) -> None:
            if tid % 2 == 0:
                for i in range(100):
                    graph_store.delete_node("T", f"d{i}")
            else:
                for i in range(100):
                    graph_store.upsert_node("T", f"new{tid}_{i}", {})

        errors = run_threads(worker, 6)
        assert errors == [], f"delete/upsert 경합 에러: {errors}"

    def test_concurrent_unicode_and_large_props(self, graph_store):
        """엣지: 유니코드 + 대형 properties 동시 쓰기 → 라운드트립 무손상."""
        big = {f"k{i}": "x" * 50 for i in range(500)}

        def worker(tid: int) -> None:
            for i in range(20):
                props = {"label": "안녕하세요 🐙", "tid": tid, **big}
                graph_store.upsert_node("T", f"u{tid}_{i}", props)

        errors = run_threads(worker, 6)
        assert errors == [], f"유니코드/대형 props 에러: {errors}"
        node = graph_store.get_node("T", "u0_0")
        assert node is not None and node["label"] == "안녕하세요 🐙"

    def test_unavailable_raises(self, graph_store):
        """에러: unavailable 스토어에서 쓰기 시 RuntimeError."""
        graph_store._available = False
        with pytest.raises(RuntimeError):
            graph_store.upsert_node("T", "x", {})


# ---------------------------------------------------------------------------
# LocalSQLDocStore — 동시 읽기/쓰기 혼합 회귀
# ---------------------------------------------------------------------------


class TestLocalSQLDocStoreConcurrency:
    def test_concurrent_read_write_mixed(self, sql_doc_store):
        """정상: 동시 upsert + list/get 읽기 → 예외 없음, 최종 카운트 일치."""
        for i in range(50):
            sql_doc_store.upsert_node_doc("s1", "T", f"seed{i}", {"i": i})

        stop = threading.Event()

        def writer(tid: int) -> None:
            for i in range(100):
                sql_doc_store.upsert_node_doc("s1", "T", f"w{tid}_{i}", {"tid": tid})
            stop.set()

        def reader(tid: int) -> None:
            while not stop.is_set():
                sql_doc_store.list_nodes(space="s1", limit=1000)
                sql_doc_store.get_node_doc("s1", "seed0")

        errors: list[Exception] = []
        lk = threading.Lock()

        def run(fn, tid):
            try:
                fn(tid)
            except Exception as exc:  # noqa: BLE001
                with lk:
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(writer, t)) for t in range(3)]
        threads += [threading.Thread(target=run, args=(reader, t)) for t in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60.0)
        assert not [th for th in threads if th.is_alive()], "데드락 의심"
        assert errors == [], f"혼합 읽기/쓰기 에러: {errors}"
        assert sql_doc_store.collection_stats()["nodes"] == 50 + 3 * 100


# ---------------------------------------------------------------------------
# LocalDocStore (레거시 JSON) — 쓰기 직렬화 + atomic read
# ---------------------------------------------------------------------------


class TestLocalDocStoreConcurrency:
    def test_concurrent_writes_serialized(self, json_doc_store):
        """정상: 8스레드 × 25 upsert → 에러 0, 200개 모두 저장."""
        N, M = 8, 25

        def worker(tid: int) -> None:
            for i in range(M):
                json_doc_store.upsert_node_doc("s1", "T", f"t{tid}_n{i}", {"tid": tid})

        errors = run_threads(worker, N)
        assert errors == [], f"동시 쓰기 에러: {errors}"
        assert json_doc_store.collection_stats()["nodes"] == N * M

    def test_concurrent_read_during_write_no_corruption(self, json_doc_store):
        """엣지: 쓰기 중 동시 읽기 → 부분/손상 JSON 미관측(예외 없음)."""
        for i in range(30):
            json_doc_store.upsert_node_doc("s1", "T", f"seed{i}", {"i": i})

        stop = threading.Event()

        def writer(tid: int) -> None:
            for i in range(80):
                json_doc_store.upsert_node_doc("s1", "T", f"w{tid}_{i}", {"tid": tid})
            stop.set()

        def reader(tid: int) -> None:
            while not stop.is_set():
                rows = json_doc_store.list_nodes(space="s1", limit=10000)
                assert isinstance(rows, list)  # 손상 시 예외 발생할 위치

        errors: list[Exception] = []
        lk = threading.Lock()

        def run(fn, tid):
            try:
                fn(tid)
            except Exception as exc:  # noqa: BLE001
                with lk:
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(writer, t)) for t in range(2)]
        threads += [threading.Thread(target=run, args=(reader, t)) for t in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60.0)
        assert not [th for th in threads if th.is_alive()], "데드락 의심"
        assert errors == [], f"읽기 중 손상 관측: {errors}"


# ---------------------------------------------------------------------------
# ChromaStore — Chroma 자체 스레드 안전 + reset_collection 핸들 보호
# ---------------------------------------------------------------------------


class TestChromaStoreConcurrency:
    def test_concurrent_add(self, chroma_store):
        """정상: 4스레드 × 10 add_texts → 에러 0, count 일치 (Chroma 스레드 안전 회귀)."""
        N, M = 4, 10

        def worker(tid: int) -> None:
            for i in range(M):
                chroma_store.add_texts(
                    texts=[f"doc {tid} {i}"],
                    metadatas=[{"tid": tid, "i": i}],
                    ids=[f"t{tid}_{i}"],
                )

        errors = run_threads(worker, N)
        assert errors == [], f"동시 add 에러: {errors}"
        assert chroma_store.count() == N * M

    def test_concurrent_resets_keep_store_valid(self, chroma_store):
        """엣지(정정된 락 검증): 여러 스레드가 reset_collection 동시 호출 →
        self._collection 핸들 교체가 직렬화되어 에러 없이 유효 상태 유지.

        락이 없으면 두 스레드가 동시에 delete_collection 하여 '이미 삭제됨' 에러나
        삭제된 컬렉션을 가리키는 손상 핸들이 남을 수 있다."""
        chroma_store.add_texts(texts=["seed"], metadatas=[{"k": "v"}], ids=["seed"])

        def worker(tid: int) -> None:
            for _ in range(5):
                chroma_store.reset_collection()

        errors = run_threads(worker, 4)
        assert errors == [], f"동시 reset 에러: {errors}"
        # 리셋 후에도 스토어가 정상 동작해야 한다.
        assert chroma_store.count() == 0
        chroma_store.add_texts(texts=["after"], metadatas=[{"k": "v"}], ids=["after"])
        assert chroma_store.count() == 1

    def test_unavailable_raises(self, chroma_store):
        """에러: unavailable 시 RuntimeError."""
        chroma_store._available = False
        with pytest.raises(RuntimeError):
            chroma_store.add_texts(texts=["x"], metadatas=[{"k": "v"}], ids=["x"])


# ---------------------------------------------------------------------------
# SqliteVecStore — 스레드-로컬 커넥션 + write 락 + WAL (LocalSQLDocStore 패턴)
# ---------------------------------------------------------------------------


class TestSqliteVecConcurrency:
    def test_concurrent_upsert(self, sqlite_vec_store):
        """정상: 4스레드 × 10 upsert → 에러 0, count 일치 (write 락 직렬화 회귀)."""
        N, M = 4, 10

        def worker(tid: int) -> None:
            for i in range(M):
                sqlite_vec_store.upsert_texts(
                    texts=[f"doc {tid} {i}"],
                    metadatas=[{"pack_id": f"p{tid}"}],
                    ids=[f"t{tid}_{i}"],
                )

        errors = run_threads(worker, N)
        assert errors == [], f"동시 upsert 에러: {errors}"
        assert sqlite_vec_store.count() == N * M

    def test_reads_during_writes(self, sqlite_vec_store):
        """핵심(WAL): 쓰기 중에도 읽기(query/count)가 차단·손상 없이 진행된다.

        Chroma 의 다중프로세스 쓰기 제약을 대체하는 SqliteVecStore 의 목표 속성 —
        라이터가 도는 중 리더는 마지막 커밋 스냅샷을 비차단으로 읽는다."""
        for i in range(10):
            sqlite_vec_store.upsert_texts(
                texts=[f"seed {i}"], metadatas=[{"pack_id": "s"}], ids=[f"seed{i}"]
            )

        def writer(tid: int) -> None:
            for i in range(15):
                sqlite_vec_store.upsert_texts(
                    texts=[f"w {tid} {i}"],
                    metadatas=[{"pack_id": "w"}],
                    ids=[f"w{tid}_{i}"],
                )

        def reader(tid: int) -> None:
            for _ in range(20):
                sqlite_vec_store.query("seed", n_results=5)
                sqlite_vec_store.count()
                sqlite_vec_store.get_by_id("seed0")

        errors: list[Exception] = []
        lock = threading.Lock()

        def wrap(fn, tid):
            try:
                fn(tid)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=wrap, args=(writer, t)) for t in range(2)]
        threads += [threading.Thread(target=wrap, args=(reader, t)) for t in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60.0)
        assert not [th for th in threads if th.is_alive()], "데드락 의심"
        assert errors == [], f"읽기/쓰기 혼합 중 손상 관측: {errors}"
        assert sqlite_vec_store.count() == 10 + 2 * 15

    def test_concurrent_resets_keep_store_valid(self, sqlite_vec_store):
        """엣지: 여러 스레드가 reset_collection 동시 호출 → write 락으로 직렬화되어
        에러 없이 유효 상태 유지."""
        sqlite_vec_store.upsert_texts(
            texts=["seed"], metadatas=[{"pack_id": "v"}], ids=["seed"]
        )

        def worker(tid: int) -> None:
            for _ in range(5):
                sqlite_vec_store.reset_collection()

        errors = run_threads(worker, 4)
        assert errors == [], f"동시 reset 에러: {errors}"
        assert sqlite_vec_store.count() == 0
        sqlite_vec_store.upsert_texts(
            texts=["after"], metadatas=[{"pack_id": "v"}], ids=["after"]
        )
        assert sqlite_vec_store.count() == 1

    def test_reads_during_reset_no_error(self, sqlite_vec_store):
        """엣지: reset_collection(DELETE 기반)이 진행되는 동안 동시 읽기(query/
        count/get_by_id)가 'no such table' 등으로 깨지지 않는다 — reset이 테이블을
        DROP하지 않고 비우기만 하므로 리더는 항상 유효한 테이블을 본다."""
        for i in range(30):
            sqlite_vec_store.upsert_texts(
                texts=[f"seed {i}"], metadatas=[{"pack_id": "s"}], ids=[f"s{i}"]
            )
        stop = threading.Event()
        errors: list[Exception] = []
        lock = threading.Lock()

        def resetter() -> None:
            for _ in range(8):
                sqlite_vec_store.reset_collection()
                for i in range(20):
                    sqlite_vec_store.upsert_texts(
                        texts=[f"r{i}"], metadatas=[{"pack_id": "s"}], ids=[f"r{i}"]
                    )
            stop.set()

        def reader() -> None:
            while not stop.is_set():
                try:
                    sqlite_vec_store.query("seed", n_results=5)
                    sqlite_vec_store.count()
                    sqlite_vec_store.get_by_id("s1")
                except Exception as exc:  # noqa: BLE001 - 어떤 예외든 수집
                    with lock:
                        errors.append(exc)

        threads = [threading.Thread(target=resetter)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60.0)
        assert not [th for th in threads if th.is_alive()], "데드락 의심"
        assert errors == [], f"reset 중 읽기 에러: {[str(e)[:80] for e in errors[:5]]}"
        # reset 이후에도 정상 동작
        assert sqlite_vec_store.count() >= 0
        sqlite_vec_store.query("seed", n_results=3)

    def test_unavailable_raises(self, sqlite_vec_store):
        """에러: unavailable 시 RuntimeError (count 는 0 반환, 예외 없음)."""
        sqlite_vec_store._available = False
        with pytest.raises(RuntimeError):
            sqlite_vec_store.upsert_texts(
                texts=["x"], metadatas=[{"pack_id": "v"}], ids=["x"]
            )
        assert sqlite_vec_store.count() == 0


# ---------------------------------------------------------------------------
# 다중 스토어 fan-out — 로더/OntologyBuilder 쓰기 패턴 대리 (데드락 부재)
# ---------------------------------------------------------------------------


class TestMultiStoreFanout:
    def test_concurrent_multistore_writes_no_deadlock(self, graph_store, sql_doc_store):
        """정상: 각 스레드가 graph + doc 스토어에 동시 쓰기 → 독립 락이라 데드락 없음."""
        N, M = 8, 40

        def worker(tid: int) -> None:
            for i in range(M):
                nid = f"t{tid}_n{i}"
                graph_store.upsert_node("T", nid, {"tid": tid})
                sql_doc_store.upsert_node_doc("s1", "T", nid, {"tid": tid})

        errors = run_threads(worker, N)
        assert errors == [], f"다중 스토어 fan-out 에러: {errors}"
        assert graph_store.count_nodes("T") == N * M
        assert sql_doc_store.collection_stats()["nodes"] == N * M
