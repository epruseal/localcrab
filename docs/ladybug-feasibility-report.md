# KùzuDB 적용 타당성 검토 보고서

**날짜**: 2026-05-27  
**브랜치**: `ladybug/feasibility`  
**결론**: **조건부 Go** — LD_PRELOAD 워크어라운드 필요

> **후속 (2026-06-14 기준): 채택 완료** — kuzu 0.11.3 설치, `KuzuGraphStore` 구현·검증 완료
> (469 passed, 0 failed). `STORAGE_MODE=kuzu`로 활성화. `madv_noop.so` LD_PRELOAD 자동 주입
> (`factory.py:_ensure_madv_noop()`). `lookup_node_type()` duck-typing 인터페이스 추가로
> `builder.add_edge` local/kuzu/neo4j 3모드 통일. 본 보고서는 의사결정 기록으로 보존.

---

## 배경

LocalGraphStore(SQLite + Python BFS)의 한계:
- `run_cypher()` 영구 no-op → `isinstance(LocalGraphStore)` 분기 10개 산재
- BFS 허브 노드 p50 = 11.86ms (43k 노드 기준, 32× 열화)

Phase 2 계획: KùzuDB(= LadybugDB 전신)로 교체 → Cypher 완전 복원 + 분기 제거

---

## 핵심 발견: 16KB 페이지 커널

```
CONFIG_PAGE_SIZE_16KB=y   # RPi5 aarch64 커널 설정
getconf PAGE_SIZE → 16384
```

KùzuDB/LadybugDB buffer manager는 **4KB 단위**로 `madvise(MADV_DONTNEED)` 를 호출한다. 16KB 페이지 커널에서 4KB-미정렬 madvise는 **EINVAL**을 반환한다. 이것이 버퍼 풀 eviction 실패의 근본 원인이다.

워크어라운드: `madv_noop.so` LD_PRELOAD → 미정렬 madvise를 noop으로 대체.

---

## 검토 결과

### 검토 1 — ARM64 설치 ✅

- **LadybugDB** 0.16.1: `pip install ladybug` 성공, ARM64 wheel 있음
- **KùzuDB** 0.11.3: `pip install kuzu` 성공, ARM64 wheel 있음 (6.8MB)
- 두 패키지 모두 동일 C++ 코드베이스 (LadybugDB = KùzuDB 공식 리브랜딩)

### 검토 2 — 스키마 DDL ✅

```cypher
CREATE NODE TABLE OntologyNode(
    node_id STRING, node_type STRING, space_id STRING, props STRING,
    PRIMARY KEY (node_id))

-- 주의: FROM...TO 는 괄호 안에 들어가야 함 (Neo4j와 다름)
CREATE REL TABLE OntologyEdge(
    FROM OntologyNode TO OntologyNode,
    relation STRING, properties STRING)
```

### 검토 3 — 데이터 덤프 ✅ (워크어라운드 필요)

**워크어라운드**: `LD_PRELOAD=madv_noop.so`

```bash
gcc -shared -fPIC -O2 -o madv_noop.so scripts/madv_noop.c -ldl
LD_PRELOAD=$(pwd)/madv_noop.so python scripts/migrate_graph_to_ladybug.py
```

**결과** (COPY FROM CSV 방식):
| 항목 | KùzuDB | SQLite 원본 | 일치 |
|------|--------|------------|------|
| 노드 | 53,810 | 53,810 | ✓ |
| 엣지 | 88,177 | 88,177 | ✓ |

**속도**: 노드 945 nodes/s, 엣지 234 edges/s

**madv_noop 부작용**: 물리 메모리가 OS로 반환되지 않음. buffer_pool_size를 명시적으로 제한해야 함 (예: `buffer_pool_size=256MB`).

### 검토 4 — Cypher 방언 호환성 ✅

| Neo4j 원본 | KùzuDB 리라이트 | 비고 |
|-----------|----------------|------|
| `MATCH (n {id: $id})` | `MATCH (n:OntologyNode {node_id: $id})` | 타입 명시 필수 |
| `labels(n)[0]` | `n.node_type` | label 함수 불필요 |
| `type(r)` | `r.relation` | 엣지 속성으로 대체 |
| `toLower()` | `lower()` | 함수명 변경 |
| `[:A\|B\|C]` | `WHERE r.relation IN [...]` | 필터로 대체 |
| `CREATE REL TABLE T FROM A TO B(...)` | `CREATE REL TABLE T(FROM A TO B, ...)` | 괄호 위치 |
| `COPY T FROM '...'` | `COPY T FROM '...' (HEADER=true)` | 헤더 옵션 명시 |

### 검토 5 — 동시성 🔄 (단일 프로세스)

직접 HTTP serve 구조: `opencrab serve --transport http`가 하나의 Python 프로세스(uvicorn 단일 워커) → 단일 이벤트 루프에서 디스패치 직렬화. KùzuDB는 다중 스레드 안전. 다중 인스턴스가 떠도 쓰기는 전용 `write.lock`(LOCK_EX)으로 직렬화되고, 읽기는 동시 진행한다.

### 검토 6 — BFS 성능 ✅

**실 데이터 53,810 노드 / 88,177 엣지 기준** (direction=out):

| 노드 차수 | SQLite BFS p50 | KùzuDB p50 | 개선배율 |
|----------|---------------|------------|---------|
| 3,294 (최대) | — | 2.94ms | — |
| 655 (기준) | 11.86ms | 2.41ms | **4.9×** |
| get_node_by_id | — | 0.57ms | — |
| depth=2 | — | 5.06ms | — |

목표 2ms에 근접 (허브 노드 기준 2.41ms). 목표치를 정확히 달성하지는 못했으나 4.9× 개선으로 실용 범위.

---

## Go/No-Go 판단

| 검토 | 조건 | 결과 |
|------|------|------|
| 1. ARM64 설치 | `pip install kuzu` 성공 + 기본 CRUD 정상 | ✅ |
| 2. 스키마 | DDL 실행 성공 | ✅ |
| 3. 덤프 | 53,810 / 88,177 100% 이전 | ✅ (LD_PRELOAD 필요) |
| 4. Cypher 동등성 | 방언 매핑 확인, COPY FROM 작동 | ✅ |
| 5. 동시성 | 단일 프로세스 구조로 문제 없음 | ✅ |
| 6. 성능 | BFS d1 p50 = 2.41ms (4.9× 개선) | ✅ (목표 2ms 근접) |

**판정: 조건부 Go**

---

## 배포 조건

### 필수

1. **`madv_noop.so` 빌드 및 배포**

```bash
gcc -shared -fPIC -O2 -o /usr/local/lib/madv_noop.so scripts/madv_noop.c -ldl
```

2. **systemd service에 LD_PRELOAD 추가**

```ini
[Service]
Environment=LD_PRELOAD=/usr/local/lib/madv_noop.so
Environment=STORAGE_MODE=kuzu
```

3. **buffer_pool_size 명시적 제한** (madv_noop 사용 시 메모리 반환 안 됨)

```python
kuzu.Database(path, buffer_pool_size=256*1024*1024)
```

### 구현 시 수정 파일 목록

| 파일 | 변경 유형 |
|------|---------|
| `opencrab/stores/kuzu_graph_store.py` | 신규 — KùzuGraphStore 클래스 |
| `opencrab/stores/factory.py` | 수정 — `make_graph_store()` kuzu 분기 추가 |
| `scripts/migrate_graph_to_ladybug.py` | 수정 — KùzuDB COPY FROM 방식으로 업데이트 |
| `scripts/madv_noop.c` | 신규 — LD_PRELOAD 워크어라운드 |
| `opencrab/ontology/rebac.py` | 수정 — isinstance 분기 2개 제거 |
| `opencrab/ontology/query.py` | 수정 — isinstance 분기 1개 제거 |
| `opencrab/ontology/builder.py` | 수정 — isinstance 분기 1개 제거 |
| `opencrab/ontology/impact.py` | 수정 — isinstance 분기 2개 제거 |
| `opencrab/mcp/tools.py` | 수정 — isinstance 분기 1개 제거 |
| `opencrab/pack/neo4j_export.py` | 수정 — isinstance 분기 1개 제거 |
| `.config/systemd/user/opencrab.service` | 수정 — LD_PRELOAD 추가 |

---

## 근본 원인 요약

```
CONFIG_PAGE_SIZE_16KB=y (RPi5 aarch64 커널)
        ↓
KùzuDB buffer manager: madvise(addr, 4096, MADV_DONTNEED)
  ← 4KB 단위 frame eviction
        ↓
커널: addr가 16KB 미정렬 → EINVAL 반환
        ↓
버퍼 풀 eviction 실패 → 풀 고갈 → RuntimeError
        ↓
워크어라운드: LD_PRELOAD=madv_noop.so
  미정렬 madvise → noop (0 반환, 물리 해제 없음)
```

LadybugDB vs KùzuDB:
- LadybugDB 0.16.1: pybind만 제공, 같은 근본 문제, **동일하게 실패**
- KùzuDB 0.11.3: pybind만 제공, 같은 근본 문제, **madv_noop 워크어라운드로 해결**
- 두 패키지 모두 COPY FROM CSV와 개별 MERGE 모두 지원
