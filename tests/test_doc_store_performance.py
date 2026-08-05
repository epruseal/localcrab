"""
성능 테스트 — LocalSQLDocStore의 성능 특성을 검증한다.

pytest-benchmark 없이 time 모듈로 직접 측정한다.
"""

from __future__ import annotations

import statistics
import time


def test_list_nodes_performance_1k(tmp_path):
    """1k 노드 환경에서 list_nodes(50000)가 200ms 이내 완료."""
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    store = LocalSQLDocStore(str(tmp_path / "doc_store.db"))
    for i in range(1000):
        store.upsert_node_doc("sp", "T", f"node_{i}", {"key": i, "name": f"Node {i}"})

    start = time.perf_counter()
    result = store.list_nodes(limit=50000)
    elapsed = time.perf_counter() - start

    assert len(result) == 1000
    assert elapsed < 0.2, f"list_nodes(1k) took {elapsed:.3f}s, expected < 0.2s"


def test_upsert_throughput_1k(tmp_path):
    """upsert_node_doc 1000건이 5초 이내 완료."""
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    store = LocalSQLDocStore(str(tmp_path / "doc_store.db"))

    start = time.perf_counter()
    for i in range(1000):
        store.upsert_node_doc("sp", "T", f"node_{i}", {"key": i})
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0, f"1000 upserts took {elapsed:.3f}s, expected < 5s"


def test_list_nodes_json_vs_sql_comparison(tmp_path):
    """LocalSQLDocStore와 LocalDocStore의 list_nodes 성능을 비교한다 (1000건 기준).

    SQLite는 트랜잭션 오버헤드가 있어 소규모 데이터셋에서는 JSON보다 느릴 수 있다.
    그러나 두 스토어 모두 절대 성능 기준(500ms)을 충족해야 하고,
    SQL이 JSON의 10배 이상 느리면 실패한다 (설계 목적 위반).
    """
    from opencrab.stores.local_doc_store import LocalDocStore
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    sql_store = LocalSQLDocStore(str(tmp_path / "sql.db"))
    json_store = LocalDocStore(str(tmp_path / "json_docs"))

    # 동일한 데이터 삽입
    for i in range(1000):
        props = {"key": i, "name": f"Node {i}"}
        sql_store.upsert_node_doc("sp", "T", f"node_{i}", props)
        json_store.upsert_node_doc("sp", "T", f"node_{i}", props)

    # 9회 측정 중앙값 — 절대 시간이 1~10ms대라 공유 CI 러너의 스케줄러 노이즈
    # 하나가 어느 한쪽에만 끼면 10배 비율 단언이 flake한다(#63 PR #100에서
    # 재관측: SQL 11ms vs JSON 1ms — 이전에 3회 최솟값으로 강건화를 시도했던
    # 바로 그 실패 시그니처가 그대로 재발했다. 최솟값은 극값 하나에 좌우되기
    # 쉬우므로 표본을 늘리고 중앙값으로 바꿔 노이즈 내성을 높인다. 로컬
    # 반복 측정(수십 회, main/이 브랜치 각각)에서 비율은 항상 1.6~2.8x
    # 대역에 머물렀고 10x 근처에도 가지 않았다 — 즉 설계상 정말 10배 가까이
    # 느려진 것이 아니라 CI 노이즈가 유발한 단발 이상치였다.
    def _median_of(fn, n=9):
        result = None
        samples = []
        for _ in range(n):
            start = time.perf_counter()
            result = fn()
            samples.append(time.perf_counter() - start)
        return result, statistics.median(samples)

    sql_result, sql_elapsed = _median_of(lambda: sql_store.list_nodes(limit=50000))
    json_result, json_elapsed = _median_of(lambda: json_store.list_nodes(limit=50000))

    ratio = json_elapsed / sql_elapsed if sql_elapsed > 0 else float("inf")
    print(f"\nSQL: {sql_elapsed:.4f}s, JSON: {json_elapsed:.4f}s, ratio: {ratio:.2f}x (JSON/SQL)")
    assert len(sql_result) == len(json_result) == 1000

    # 두 스토어 모두 500ms 이내여야 한다
    assert sql_elapsed < 0.5, f"SQL list_nodes took {sql_elapsed:.3f}s, expected < 0.5s"
    assert json_elapsed < 0.5, f"JSON list_nodes took {json_elapsed:.3f}s, expected < 0.5s"

    # SQL이 JSON의 10배 이상 느리면 설계 목적(BM25 캐시 hot path) 위반
    assert sql_elapsed < json_elapsed * 10, (
        f"SQL ({sql_elapsed:.3f}s) is more than 10x slower than JSON ({json_elapsed:.3f}s). "
        "SQLite overhead is unexpectedly high."
    )


def test_get_node_doc_performance(tmp_path):
    """get_node_doc 단건 조회가 50ms 이내."""
    from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

    store = LocalSQLDocStore(str(tmp_path / "doc_store.db"))
    for i in range(1000):
        store.upsert_node_doc("sp", "T", f"node_{i}", {"key": i})

    start = time.perf_counter()
    result = store.get_node_doc("sp", "node_500")
    elapsed = time.perf_counter() - start

    assert result is not None
    assert elapsed < 0.05, f"get_node_doc took {elapsed:.3f}s, expected < 0.05s"
