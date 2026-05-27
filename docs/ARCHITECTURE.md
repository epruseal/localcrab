# LocalCrab Store Architecture

## 목차

1. [스토어 구조](#1-스토어-구조)
2. [LocalSQLDocStore 선택 근거](#2-localsqldocstore-선택-근거)
3. [DuckDB 검토 결과 — 기각](#3-duckdb-검토-결과--기각)
4. [LadybugDB Phase 2 로드맵](#4-ladybugdb-phase-2-로드맵)
5. [마이그레이션 절차](#5-마이그레이션-절차)
6. [BM25 커버리지 경고](#6-bm25-커버리지-경고)
7. [SQLite 버전 요구사항](#7-sqlite-버전-요구사항)

---

## 1. 스토어 구조

LocalCrab은 `STORAGE_MODE` 환경변수로 두 가지 백엔드를 선택한다.

| 스토어 역할 | local 모드 | docker 모드 |
| --- | --- | --- |
| 그래프 | `LocalGraphStore` (`graph.db`, SQLite) | `Neo4jStore` (`bolt://localhost:7687`) |
| 문서 | `LocalDocStore` (`docs/*.json`, JSON 파일) | `MongoStore` (MongoDB) |
| 벡터 | `ChromaStore` (PersistentClient, `chroma/`) | `ChromaStore` (HttpClient) |
| SQL | `SQLStore` (`opencrab.db`, SQLite) | `SQLStore` (PostgreSQL) |

> **현재 상태**: `LocalSQLDocStore` (SQLite 기반 문서 스토어)가 구현 완료되어
> `opencrab/stores/local_sql_doc_store.py`에 존재하지만, `factory.py`의
> `make_doc_store()`는 아직 `LocalDocStore` (JSON)를 반환한다.
> factory 연결은 Phase 2 작업 항목이다.

### 팩토리 (`opencrab/stores/factory.py`)

```
make_graph_store(settings)
    is_local → LocalGraphStore(db_path="<LOCAL_DATA_DIR>/graph.db")
    else     → Neo4jStore(uri=NEO4J_URI, ...)

make_doc_store(settings)
    is_local → LocalDocStore(data_dir="<LOCAL_DATA_DIR>/docs")   # 현재
    else     → MongoStore(uri=MONGODB_URI, db_name=MONGODB_DB)

make_vector_store(settings)
    → ChromaStore(local_mode=is_local, local_path="<LOCAL_DATA_DIR>/chroma")

make_sql_store(settings)
    is_local → SQLStore(url="sqlite:///<LOCAL_DATA_DIR>/opencrab.db")
    else     → SQLStore(url=POSTGRES_URL)
```

`LOCAL_DATA_DIR` 기본값: `/home/asdf/.openclaw/workspace/data/localcrab`

---

## 2. LocalSQLDocStore 선택 근거

### 문제: JSON 파일의 O(N) 전체 로드

`LocalDocStore`는 모든 읽기·쓰기에서 JSON 파일 전체를 메모리에 올린다.

- `_load()`: 전체 파일 역직렬화 — O(N)
- `_save()`: 전체 dict 직렬화 후 atomic rename — O(N)
- `list_nodes(limit=50000)`: 전체 파일 로드 후 슬라이스 — O(N)

### 핫 패스: BM25 캐시 재구성

`ontology_query` 도구는 쿼리마다 BM25 인덱스의 fingerprint를 확인하고, 캐시가
무효화되면 즉시 재구성한다.

```python
# opencrab/ontology/query.py
_BM25_NODE_LIMIT = int(os.getenv("OPENCRAB_BM25_NODE_LIMIT", "50000"))

nodes = self._doc_store.list_nodes(limit=_BM25_NODE_LIMIT)  # 핫 패스
self._bm25_cache = BM25Index.build(nodes)
```

이 경로에서 JSON 파일 백엔드는 매 쿼리마다 `nodes.json` 전체를 파싱한다. 데이터가
늘어날수록 지연이 선형으로 증가한다.

### 성능 비교 (예측)

| 데이터 규모 | JSON `list_nodes` | SQLite `list_nodes` |
| --- | --- | --- |
| 43k 노드 (현재) | ~400ms (파일 전체 파싱) | ~일정 (LIMIT k 행만 읽음) |
| 430k 노드 (10x) | ~4s+ (선형 열화) | ~일정 (LIMIT k 행만 읽음) |

SQLite B-tree는 `SELECT ... LIMIT k`로 앞에서 k행만 읽으므로, 테이블 전체 크기에
무관하게 응답 시간이 일정하게 유지된다.

### upsert 복잡도 비교

| 연산 | JSON (`LocalDocStore`) | SQLite (`LocalSQLDocStore`) |
| --- | --- | --- |
| `upsert_node_doc` | O(N): 전체 재직렬화 | O(log N): `INSERT OR REPLACE` |
| `get_node_doc` | O(N): 전체 파싱 + dict.get | O(log N): PK lookup |
| `delete_node_doc` | O(N): 전체 로드 + 재저장 | O(log N): DELETE by PK |
| `collection_stats` | O(N): `len(json.load())` | O(1): `COUNT(*)` (B-tree 내부) |

### LocalSQLDocStore 스키마

```sql
-- 노드 문서 (space x node_id PK)
CREATE TABLE doc_nodes (
    space       TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    node_type   TEXT NOT NULL DEFAULT '',
    properties  TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (space, node_id)
);
CREATE INDEX idx_doc_nodes_updated ON doc_nodes(updated_at);

-- 소스 레코드
CREATE TABLE doc_sources (
    source_id   TEXT PRIMARY KEY,
    text        TEXT NOT NULL DEFAULT '',
    metadata    TEXT NOT NULL DEFAULT '{}',
    ingested_at TEXT NOT NULL
);

-- 감사 로그 (uuid4 PK, timestamp DESC 인덱스)
CREATE TABLE audit_log (
    event_id    TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    subject_id  TEXT,
    details     TEXT NOT NULL DEFAULT '{}',
    timestamp   TEXT NOT NULL
);
CREATE INDEX idx_audit_ts ON audit_log(timestamp DESC);
```

`properties` / `metadata` / `details`는 JSON TEXT로 저장한다. `json_extract()`
의존성(SQLite 3.38+)을 피하고 버전 요구사항을 3.9.0+로 유지하기 위해 파싱은
Python `json.loads()`로 처리한다.

---

## 3. DuckDB 검토 결과 — 기각

### 검토 배경

`LocalDocStore`의 JSON O(N) 문제를 해결할 대안으로 DuckDB를 검토했다.

### 기각 근거

| 항목 | 분석 |
| --- | --- |
| 워크로드 유형 | OLTP — upsert / PK lookup / LIMIT 조회 |
| DuckDB 강점 | OLAP — GROUP BY, 집계, 컬럼형 스캔 |
| 이 워크로드에서의 차이 | DuckDB vs SQLite 성능 차이 없음 |
| 추가 의존성 | `pip install duckdb` 필요 |
| 결론 | 추가 의존성 대비 실익 없음 — 기각 |

doc 스토어의 핵심 연산(`upsert_node_doc`, `get_node_doc`, `list_nodes LIMIT k`)은
행 단위 OLTP이다. DuckDB의 컬럼형 저장 구조는 이 패턴에서 SQLite 대비 유의미한
이점을 제공하지 않는다.

---

## 4. LadybugDB Phase 2 로드맵

### Phase 전략

```
Phase 1 (완료): Neo4j → LocalGraphStore (SQLite BFS)
    목표: Docker 없이 로컬 실행 가능, 안정성 최우선
    결과: MCP 도구 전체 로컬 동작 확보

Phase 2 (예정): LocalGraphStore → LadybugDB (임베디드 컬럼형 그래프 DB)
    목표: Cypher 완전 복원, 우회코드 전면 제거
```

### 현재 LocalGraphStore의 한계

`run_cypher()`는 영구 no-op이다 (`local_graph_store.py:254`):

```python
def run_cypher(self, cypher: str, params=None) -> list[dict]:
    """Not supported in local mode — returns empty list with a warning."""
    logger.warning("run_cypher() is not supported in local mode; returning [].")
    return []
```

이로 인해 다음 우회코드가 필요하다.

| 기능 | 우회 방법 |
| --- | --- |
| `content_pack_list` | `LocalGraphStore.list_packs()` — SQLite GROUP BY `json_extract` |
| `ontology_lever_simulate` | `LocalGraphStore.find_by_relations()` — 1-홉 relation 필터 |
| `export` | `LocalGraphStore.export_nodes()` / `export_edges()` |
| `analyse` | `LocalGraphStore.get_node_by_id()` |

코드베이스 전반에 `isinstance(graph, LocalGraphStore)` 분기가 존재한다. 또한
`find_neighbors()`는 Cypher 가변 관계 패턴(`*1..N`) 대신 Python BFS로 구현되어 있어,
허브 노드(차수 수백 이상)에서 성능 열화가 발생한다 (`bench_graph_backends.py`
실측: 43k 노드 / 최고 차수 615에서 d1 p50 = 11.86ms, 20k 대비 32× 급등).

### LadybugDB 전환 시 기대 효과

- KùzuDB 포크, 임베디드 컬럼형 그래프 DB
- Cypher 네이티브 지원 → `run_cypher()` no-op 제거
- `list_packs`, `find_by_relations`, `export_nodes/edges`, `get_node_by_id` 전부 Cypher로 대체
- `isinstance(graph, LocalGraphStore)` 분기 전면 제거
- Python BFS → Cypher 가변 관계 패턴(`-[*1..N]-`)으로 교체
- 스토어 추상화 복원 (그래프 / doc 스토어가 동일 인터페이스만 구현)

### 전환 전 필수 검증 체크리스트

- [ ] LadybugDB의 Cypher 방언이 Neo4j Cypher와 호환되는지 확인 (`run_cypher()` 호출 시그니처 동일 여부)
- [ ] `find_neighbors()` 결과 집합이 Neo4j 모드와 동등한지 검증 (Jaccard 유사도 기준)
- [ ] MCP 서버 멀티스레드 환경에서 임베디드 DB의 동시 접근 안전성 검증
- [ ] 대규모 그래프 (430k+ 노드) 에서 LadybugDB 인덱스 빌드 시간 측정
- [ ] 기존 `graph.db` (SQLite) → LadybugDB 마이그레이션 스크립트 작성 및 검증
- [ ] `list_packs`, `export_nodes/edges`, `find_by_relations` Cypher 쿼리 방언 차이 확인

---

## 5. 마이그레이션 절차

### Docker → local 전환

현재 `scripts/` 디렉토리에 `migrate_to_local.py`는 없다. 수동 전환 절차:

**1. 환경변수 전환**

```bash
export STORAGE_MODE=local
export LOCAL_DATA_DIR=/your/data/dir   # 기본: ~/.openclaw/workspace/data/localcrab
```

**2. 데이터 디렉토리 확인**

```
<LOCAL_DATA_DIR>/
  graph.db          # LocalGraphStore (SQLite)
  graph.db-wal      # WAL 파일 — 백업 시 반드시 포함
  graph.db-shm      # 공유 메모리 파일 — 백업 시 반드시 포함
  docs/             # LocalDocStore (JSON 파일)
    nodes.json
    sources.json
    audit_log.json
  chroma/           # Chroma PersistentClient
  opencrab.db       # SQLStore (SQLite)
```

**3. 백업**

WAL 모드 사용 시 `graph.db`만 복사하면 데이터 손실이 발생할 수 있다. 세 파일을
함께 복사해야 한다.

```bash
cp graph.db graph.db-wal graph.db-shm /backup/path/
cp opencrab.db /backup/path/
cp -r docs/ /backup/path/docs/
```

**4. 검증**

```bash
opencrab status
opencrab manifest
opencrab query "test"
```

`status` 출력에서 `LocalGraphStore`, `LocalDocStore`, `ChromaStore (local)` 가
표시되면 정상이다.

---

## 6. BM25 커버리지 경고

### 현재 설정

```python
# opencrab/ontology/query.py
_BM25_NODE_LIMIT = int(os.getenv("OPENCRAB_BM25_NODE_LIMIT", "50000"))
```

BM25 인덱스는 doc 스토어에서 최대 `_BM25_NODE_LIMIT`개 노드만 로드한다. 이 값은
인덱스 빌드 시간과 메모리 사용량을 제한하기 위한 상한이다.

### 대규모 데이터 환경에서의 영향

| 총 노드 수 | BM25 인덱싱 비율 | 비고 |
| --- | --- | --- |
| 43,000 (현재) | 100% | 전체 커버 |
| 50,000 | 100% | 한계선 |
| 430,000 (10x) | 11.6% | 88.4% 노드가 BM25 검색에서 누락 |

BM25 미커버 노드는 벡터 검색(Chroma)에서는 여전히 검색 가능하다. 그러나 키워드
정밀도가 높은 쿼리에서 BM25 결과가 RRF 재랭킹에 기여하지 못해 검색 품질이 저하될
수 있다.

### 조정 방법

```bash
# 인덱스 한도를 100,000으로 올림 (메모리 및 빌드 시간 증가)
export OPENCRAB_BM25_NODE_LIMIT=100000
opencrab serve
```

한도를 올리기 전에 인덱스 빌드 시간을 측정해야 한다. `LocalSQLDocStore`로 전환하면
`list_nodes(limit=N)` 호출 자체의 지연은 N에 무관하게 유지되지만, BM25 인덱스
빌드(`BM25Index.build(nodes)`)는 여전히 노드 수에 비례한 CPU 비용이 발생한다.

---

## 7. SQLite 버전 요구사항

### 최소 버전: SQLite 3.9.0

로컬 모드는 `json_extract()` 함수를 사용한다 (`local_graph_store.py`의 DDL 및
`list_packs()`, `export_nodes()` 메서드). `json_extract()`는 SQLite 3.9.0
(2015-10-14 출시)부터 지원된다.

사용처:

```python
# local_graph_store.py — DDL
"CREATE INDEX IF NOT EXISTS idx_nodes_pack"
" ON graph_nodes(json_extract(properties, '$.pack_id'))"

# list_packs() 메서드
"SELECT json_extract(properties, '$.pack_id') AS pack_id ..."
```

### 버전 확인

```bash
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
```

3.9.0 미만이면 로컬 모드 초기화 시 인덱스 생성이 실패하고 `LocalGraphStore`가
`available=False`로 설정된다.

`LocalSQLDocStore`는 `json_extract()`를 사용하지 않으므로 (properties 파싱은
Python `json.loads()`로 처리) 동일한 3.9.0+ 요구사항이 적용되지만 추가 제약은
없다.
