# LadybugDB 적용 타당성 검토 보고서

**날짜**: 2026-05-27  
**브랜치**: `ladybug/feasibility`  
**결론**: **No-Go** — 현재 버전(0.16.1)에서 이 시스템(RPi5 aarch64)에 미적용

---

## 배경

LocalGraphStore(SQLite + Python BFS)의 한계:
- `run_cypher()` 영구 no-op → `isinstance(LocalGraphStore)` 분기 10개 산재
- BFS 허브 노드 p50 = 11.86ms (43k 노드 기준, 32× 열화)

Phase 2 계획: LadybugDB(KùzuDB 공식 리브랜딩)로 교체 → Cypher 완전 복원 + 분기 제거

---

## 검토 결과

### 검토 1 — ARM64 설치 및 기본 동작 ✅

```
ladybug-0.16.1-cp313-cp313-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl
```

- `pip install ladybug` 성공
- 기본 CRUD (`CREATE NODE TABLE`, `MERGE`, `MATCH`) 소규모(~100개) 정상 작동
- 버전: 0.16.1

### 검토 2 — Option A 스키마 DDL ✅

단일 범용 타입 스키마 정상 실행:

```cypher
CREATE NODE TABLE OntologyNode(
    node_id STRING, node_type STRING, space_id STRING, props STRING,
    PRIMARY KEY (node_id))

CREATE REL TABLE OntologyEdge(
    FROM OntologyNode TO OntologyNode,
    relation STRING, properties STRING)
```

**주의**: Neo4j/KùzuDB 문서와 다른 문법 — `CREATE REL TABLE ... FROM ... TO ...`는 `CREATE REL TABLE ...(FROM ... TO ..., ...)` 형식으로 수정 필요.

### 검토 3 — 데이터 덤프 ❌ BLOCKED

**증상**: ~12,000–15,000개 노드 삽입 후 반복적으로 충돌:
```
RuntimeError: Buffer manager exception:
  Releasing physical memory associated with a frame failed with error code -1: Invalid argument.
```

**시도한 우회책 모두 실패**:

| 방법 | 결과 |
|------|------|
| `buffer_pool_size=256MB` | 실패 (같은 오류) |
| `buffer_pool_size=2GB` | 실패 (같은 오류) |
| LD_PRELOAD `MADV_FREE→MADV_DONTNEED` | 실패 (DONTNEED도 EINVAL) |
| tmpfs vs nvme 파일시스템 변경 | 실패 (동일) |
| N개마다 연결 재오픈 | 실패 (15k 이후 `std::bad_alloc` + SEGFAULT) |
| `COPY FROM CSV` | 실패 (같은 오류) |
| `create_arrow_table` bulk | 노드만 성공, 엣지 조회 불가 (인덱스 없는 스캔 테이블) |

### 근본 원인 분석

**`MADV_FREE`가 이 시스템에서 실패**:

```
$ strace -e madvise python -c "..."
madvise(0x7fff..., 4096, MADV_DONTNEED) = -1 EINVAL (Invalid argument)
```

```python
# Python 직접 검증
libc.madvise(addr, size, MADV_FREE)  # ret=-1, errno=22 (EINVAL)
libc.madvise(addr, size, MADV_DONTNEED)  # ret=0 (성공)
```

**메커니즘**:
1. LadybugDB의 pybind 백엔드(유일하게 설치된 백엔드)가 버퍼 풀에 `madvise(MADV_FREE)` 사용
2. 이 시스템 ARM64 aarch64 / kernel 6.18.29+rpt-rpi-2712에서 `MADV_FREE`가 `EINVAL` 반환
3. LD_PRELOAD로 `MADV_DONTNEED`로 변환해도 `madvise`가 해당 메모리 영역에서 `EINVAL`
4. 페이지 eviction 불가 → 버퍼 풀이 ~12,000-15,000개 노드 이후 고갈
5. Python 예외 발생

**C-API 백엔드 대안 없음**:
- LadybugDB 0.16.1은 `liblbug.so` 공유 라이브러리를 별도 제공 (ARM64용 미포함)
- `backend='capi'` 강제 시 `RuntimeError: Could not find lbug C API shared library`

### 검토 4–6 — 미실시

덤프 자체가 불가하므로 Cypher 동등성, 동시성, 성능 검토 미실시.

---

## Go/No-Go 판단

| 검토 | 조건 | 결과 |
|------|------|------|
| 1. ARM64 설치 | `pip install ladybug` 성공 + 기본 CRUD 정상 | ✅ |
| 2. 스키마 | OntologyNode/Edge DDL 실행 성공 | ✅ |
| 3. 덤프 | 53,810 노드 / 88,177 엣지 100% 이전 | ❌ **BLOCKED** |
| 4. Cypher 동등성 | 5개 쿼리 Jaccard ≥ 0.95 | ⏭ 미실시 |
| 5. 동시성 | 5 프로세스 동시 쓰기 오류 없음 | ⏭ 미실시 |
| 6. 성능 | BFS d1 p50 < 2ms | ⏭ 미실시 |

**판정: No-Go**

---

## 권고사항

### 즉시 (Phase 2 연기)

LocalGraphStore(SQLite BFS) 유지. `isinstance` 분기 10개는 현재 상태 유지.

### 재검토 조건

아래 중 하나가 충족될 때 재검토:

1. **LadybugDB C-API 공유 라이브러리 ARM64 제공**  
   `liblbug.so` aarch64 빌드가 PyPI 또는 GitHub Release에 배포되면 C-API 백엔드로 우회 가능.

2. **버퍼 매니저 수정**  
   LadybugDB가 `MADV_FREE` 실패를 gracefully 처리하거나 `MADV_DONTNEED`만 사용하도록 수정.

3. **x86-64 환경 배포**  
   배포 환경이 x86-64로 변경되면 이 문제가 사라질 가능성 있음.

### BFS 성능이 긴급 문제라면

별도 최적화 방안 (`fix/bfs-hub-early-termination` 브랜치 참조):
- 허브 노드 조기 종료 (이미 구현됨)
- `find_neighbors` 결과 캐싱 (LRU, TTL 기반)
- 인접 리스트 테이블 선계산 (graph_neighbors materialized view)

---

## 참고: Cypher 문법 차이 (구현 시 참고용)

향후 재검토 시 필요한 리라이트 매핑:

| Neo4j 원본 | LadybugDB 리라이트 |
|-----------|------------------|
| `MATCH (n {id: $id})` | `MATCH (n:OntologyNode {node_id: $id})` |
| `labels(n)[0]` | `n.node_type` |
| `type(r)` | `r.relation` |
| `toLower()` | `lower()` |
| `[:A\|B\|C]` | `WHERE r.relation IN [...]` |
| `CREATE REL TABLE T FROM A TO B(...)` | `CREATE REL TABLE T(FROM A TO B, ...)` |
