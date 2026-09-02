"""
성능 특성 테스트 — LocalSQLDocStore 와 LocalDocStore 의 성능 계약을 검증한다.

pytest-benchmark 없이 time 모듈로 직접 측정한다. 공유 CI 러너에서도 판정이
흔들리지 않도록 단언을 세 층으로 나눈다(#157, #275).

1. 결정적 단언: EXPLAIN QUERY PLAN 과 sqlite3 trace callback 으로 "정렬을
   인덱스 워크로 처리한다", "행 수와 무관하게 문장 1건이다" 같은 계약을 시계와
   무관하게 고정한다.
2. CPU 시간 단언: 알고리즘 비용(선형 스케일, 행당 비용 예산)은
   ``time.process_time()`` 최솟값으로 잰다. 다른 프로세스가 CPU 를 빼앗는
   가산 지연은 CPU 시간에 더해지지 않고, 주파수 저하 같은 곱셈 감속은 같은
   경로 두 크기의 비율을 보존한다.
3. 벽시계 단언: sleep, 락 대기처럼 CPU 시간에 잡히지 않는 지연은
   ``time.perf_counter()`` 최솟값으로 잡는다. 상한 숫자는 원래 파일의 값을
   그대로 두고 단발 측정만 5회 최솟값으로 바꿨다. 최솟값은 위쪽 극값에
   영향받지 않으므로 스케줄링 한 번으로는 깨지지 않는다.

서로 다른 코드 경로(JSON 대 SQL)의 밀리초 측정값 비율은 러너 노이즈와 같은
자릿수라 단언하지 않는다. 그 비율은 정보로만 출력한다.

검출력 증명(역변이 R1~R9)과 부하 시뮬레이션 재현 절차는 PR 본문에 있다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from opencrab.stores.local_doc_store import LocalDocStore
from opencrab.stores.local_sql_doc_store import LocalSQLDocStore

SAMPLES = 5
SMALL_N = 1000
LARGE_N = 16000

# 같은 경로를 1k 와 16k 행에서 잰 CPU 시간 비율의 상한. 선형이면 16 이고 캐시
# 효과가 얹히면 20 안팎이다. O(n²) 는 행마다 전체 행을 한 번 더 훑기만 해도
# 150 을 넘는다. 60 은 크기별 차등 감속을 3배 허용하면서 O(n²) 를 3배 여유로
# 잡는다.
SCALING_RATIO_MAX = 60.0

# CPU 예산: 행당 CPU 비용이 자릿수 단위로 늘어나는 회귀를 잡는다. 정상 구현은
# 각 예산의 1/20 이하다.
LIST_16K_CPU_BUDGET_S = 2.0
LIST_1K_CPU_BUDGET_S = 0.2
UPSERT_1K_CPU_BUDGET_S = 2.0

# 벽시계 상한: 원래 파일의 값 그대로다. sleep 과 락 대기 같은 비 CPU 지연을
# 잡는다.
LIST_1K_WALL_MAX_S = 0.2
GET_WALL_MAX_S = 0.05
COMPARISON_WALL_MAX_S = 0.5
UPSERT_1K_WALL_MAX_S = 5.0

# upsert_node_doc 1건은 BEGIN, INSERT ... ON CONFLICT, COMMIT 의 문장 3개다.
UPSERT_STATEMENTS = 3


def _props(i: int) -> dict[str, Any]:
    return {"key": i, "name": f"Node {i}"}


def _seed_sql(path: str, n: int) -> LocalSQLDocStore:
    store = LocalSQLDocStore(path)
    for i in range(n):
        store.upsert_node_doc("sp", "T", f"node_{i}", _props(i))
    return store


def _seed_json(path: str, n: int) -> LocalDocStore:
    """LocalDocStore.upsert_node_doc 는 호출마다 파일 전체를 다시 쓰므로 n건
    적재가 O(n²) 다(16k 는 수 분). 스토어 자체의 직렬화 경로(``_save``)로
    완성 픽스처를 한 번에 기록해 파일 포맷 지식을 테스트에 복제하지 않는다."""
    store = LocalDocStore(path)
    store._save(
        "nodes",
        {
            f"sp::node_{i}": {
                "space": "sp",
                "node_type": "T",
                "node_id": f"node_{i}",
                "properties": _props(i),
            }
            for i in range(n)
        },
    )
    return store


def _min_of(
    fn: Callable[[], Any], clock: Callable[[], float], n: int = SAMPLES
) -> tuple[Any, float]:
    """fn 을 n회 실행해 (마지막 결과, clock 기준 최솟값 소요 시간) 을 돌려준다."""
    best = float("inf")
    result = None
    for _ in range(n):
        start = clock()
        result = fn()
        best = min(best, clock() - start)
    return result, best


def _statements(store: LocalSQLDocStore, fn: Callable[[], Any]) -> tuple[Any, list[str]]:
    """fn 실행 중 SQLite 가 실제로 실행한 문장(바인딩 값이 치환된 텍스트)을 모은다."""
    statements: list[str] = []
    conn = store._conn
    conn.set_trace_callback(statements.append)
    try:
        result = fn()
    finally:
        conn.set_trace_callback(None)
    return result, statements


def _query_plan(store: LocalSQLDocStore, statement: str) -> str:
    rows = store._conn.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
    return "\n".join(str(row[3]) for row in rows)


@pytest.fixture(scope="module")
def scaled_stores(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[int, Any]]:
    """백엔드별 1k, 16k 스토어. 읽기 전용으로만 쓴다(행 수가 단언 대상이다)."""
    root = tmp_path_factory.mktemp("perf")
    return {
        "sql": {n: _seed_sql(str(root / f"sql_{n}.db"), n) for n in (SMALL_N, LARGE_N)},
        "json": {n: _seed_json(str(root / f"json_{n}"), n) for n in (SMALL_N, LARGE_N)},
    }


# ----------------------------------------------------------------------
# 1. 결정적 단언
# ----------------------------------------------------------------------


def test_list_nodes_plan_and_statement_count(scaled_stores):
    """space 미지정 list_nodes 는 문장 1건이고, 정렬을 idx_doc_nodes_updated_tiebreak
    인덱스 워크로 처리한다(임시 B-tree 정렬 없음, PR #100 의 설계)."""
    store = scaled_stores["sql"][SMALL_N]

    result, statements = _statements(store, lambda: store.list_nodes(limit=50000))

    assert len(result) == SMALL_N
    assert len(statements) == 1, statements
    plan = _query_plan(store, statements[0])
    assert "USING INDEX idx_doc_nodes_updated_tiebreak" in plan, plan
    assert "TEMP B-TREE" not in plan, plan


def test_get_node_doc_plan_and_statement_count(scaled_stores):
    """get_node_doc 는 문장 1건이고 PK 인덱스 SEARCH 다(전체 SCAN 아님)."""
    store = scaled_stores["sql"][SMALL_N]

    result, statements = _statements(store, lambda: store.get_node_doc("sp", "node_500"))

    assert result is not None
    assert len(statements) == 1, statements
    plan = _query_plan(store, statements[0])
    assert "SEARCH doc_nodes" in plan, plan
    assert "SCAN" not in plan, plan


def test_upsert_is_single_statement_transaction(tmp_path):
    """upsert 1건은 문장 3개(BEGIN, INSERT ... ON CONFLICT, COMMIT)이며 테이블
    크기와 무관하다. 상대 비교가 아니라 절대 형상을 단언해 "항상 추가 조회를
    한 번 더 한다" 같은 회귀도 잡는다."""
    empty = LocalSQLDocStore(str(tmp_path / "empty.db"))
    seeded = _seed_sql(str(tmp_path / "seeded.db"), SMALL_N)

    for store in (empty, seeded):
        _, statements = _statements(
            store, lambda store=store: store.upsert_node_doc("sp", "T", "node_extra", _props(-1))
        )
        assert len(statements) == UPSERT_STATEMENTS, statements


# ----------------------------------------------------------------------
# 2. CPU 시간 단언
# ----------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["sql", "json"])
def test_list_nodes_scales_linearly(scaled_stores, backend):
    """list_nodes 의 CPU 비용이 행 수에 대해 초선형으로 자라지 않는다.

    1k 와 16k 를 번갈아 재서 지속 감속이 두 크기에 같이 걸리게 하고, 각각의
    최솟값 비율을 단언한다."""
    small = scaled_stores[backend][SMALL_N]
    large = scaled_stores[backend][LARGE_N]
    small_cpu = large_cpu = float("inf")
    for _ in range(SAMPLES):
        _, t = _min_of(lambda: small.list_nodes(limit=50000), time.process_time, n=1)
        small_cpu = min(small_cpu, t)
        _, t = _min_of(lambda: large.list_nodes(limit=50000), time.process_time, n=1)
        large_cpu = min(large_cpu, t)

    ratio = large_cpu / max(small_cpu, 1e-4)
    print(f"\n{backend}: cpu 1k={small_cpu:.4f}s 16k={large_cpu:.4f}s ratio={ratio:.1f}")
    assert ratio < SCALING_RATIO_MAX, (
        f"{backend} list_nodes CPU grew {ratio:.1f}x from {SMALL_N} to {LARGE_N} rows"
        f" (limit {SCALING_RATIO_MAX}); superlinear regression"
    )


@pytest.mark.parametrize("backend", ["sql", "json"])
def test_list_nodes_cpu_budget_16k(scaled_stores, backend):
    """16k 행 list_nodes 의 CPU 시간 예산. 행당 CPU 비용이 자릿수 단위로 늘면 깨진다."""
    store = scaled_stores[backend][LARGE_N]

    result, cpu = _min_of(lambda: store.list_nodes(limit=50000), time.process_time)

    assert len(result) == LARGE_N
    assert cpu < LIST_16K_CPU_BUDGET_S, (
        f"{backend} list_nodes({LARGE_N}) used {cpu:.3f}s CPU, budget {LIST_16K_CPU_BUDGET_S}s"
    )


def test_upsert_throughput_1k(tmp_path):
    """upsert_node_doc 1000건: CPU 예산 안이고(행당 비용 회귀), 벽시계 상한 안이다(커밋 대기)."""
    store = LocalSQLDocStore(str(tmp_path / "doc_store.db"))

    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    for i in range(SMALL_N):
        store.upsert_node_doc("sp", "T", f"node_{i}", {"key": i})
    cpu = time.process_time() - cpu_start
    wall = time.perf_counter() - wall_start

    assert cpu < UPSERT_1K_CPU_BUDGET_S, (
        f"1000 upserts used {cpu:.3f}s CPU, budget {UPSERT_1K_CPU_BUDGET_S}s"
    )
    assert wall < UPSERT_1K_WALL_MAX_S, (
        f"1000 upserts took {wall:.3f}s, expected < {UPSERT_1K_WALL_MAX_S}s"
    )


# ----------------------------------------------------------------------
# 3. 벽시계 단언 (비 CPU 지연)
# ----------------------------------------------------------------------


def test_list_nodes_performance_1k(scaled_stores):
    """1k 노드 환경에서 list_nodes(50000) 벽시계 최솟값이 200ms 이내."""
    store = scaled_stores["sql"][SMALL_N]

    result, wall = _min_of(lambda: store.list_nodes(limit=50000), time.perf_counter)

    assert len(result) == SMALL_N
    assert wall < LIST_1K_WALL_MAX_S, (
        f"list_nodes(1k) took {wall:.3f}s, expected < {LIST_1K_WALL_MAX_S}s"
    )


def test_get_node_doc_performance(scaled_stores):
    """get_node_doc 단건 조회 벽시계 최솟값이 50ms 이내."""
    store = scaled_stores["sql"][SMALL_N]

    result, wall = _min_of(lambda: store.get_node_doc("sp", "node_500"), time.perf_counter)

    assert result is not None
    assert wall < GET_WALL_MAX_S, f"get_node_doc took {wall:.3f}s, expected < {GET_WALL_MAX_S}s"


def test_list_nodes_json_vs_sql_comparison(scaled_stores):
    """두 백엔드가 같은 절대 예산(CPU 200ms, 벽시계 500ms)을 만족한다 (1000건 기준).

    SQLite 는 트랜잭션과 인덱스 워크 오버헤드가 있어 소규모에서는 JSON 보다
    느릴 수 있다. 두 경로의 비율은 러너 노이즈와 같은 자릿수라 단언하지 않고
    정보로만 출력한다. "SQLite 오버헤드가 폭주하지 않는다" 는
    test_list_nodes_plan_and_statement_count 와 SQL 쪽 CPU 예산이 잡는다."""
    sql_store = scaled_stores["sql"][SMALL_N]
    json_store = scaled_stores["json"][SMALL_N]

    sql_result, sql_cpu = _min_of(lambda: sql_store.list_nodes(limit=50000), time.process_time)
    json_result, json_cpu = _min_of(lambda: json_store.list_nodes(limit=50000), time.process_time)
    _, sql_wall = _min_of(lambda: sql_store.list_nodes(limit=50000), time.perf_counter)
    _, json_wall = _min_of(lambda: json_store.list_nodes(limit=50000), time.perf_counter)

    ratio = sql_cpu / json_cpu if json_cpu > 0 else float("inf")
    print(
        f"\nSQL cpu={sql_cpu:.4f}s wall={sql_wall:.4f}s, JSON cpu={json_cpu:.4f}s"
        f" wall={json_wall:.4f}s, SQL/JSON cpu ratio={ratio:.2f}x (info only)"
    )
    assert len(sql_result) == len(json_result) == SMALL_N

    for name, cpu, wall in (("SQL", sql_cpu, sql_wall), ("JSON", json_cpu, json_wall)):
        assert cpu < LIST_1K_CPU_BUDGET_S, (
            f"{name} list_nodes used {cpu:.3f}s CPU, budget {LIST_1K_CPU_BUDGET_S}s"
        )
        assert wall < COMPARISON_WALL_MAX_S, (
            f"{name} list_nodes took {wall:.3f}s, expected < {COMPARISON_WALL_MAX_S}s"
        )
