# LocalCrab Store Architecture

## 목차

1. [스토어 구조](#1-스토어-구조)
2. [LocalSQLDocStore 선택 근거](#2-localsqldocstore-선택-근거)
3. [DuckDB 검토 결과 — 기각](#3-duckdb-검토-결과--기각)
4. [Kuzu(ladybug) 그래프 스토어 — 현황](#4-kuzuladybug-그래프-스토어--현황)
5. [마이그레이션 절차](#5-마이그레이션-절차)
6. [BM25 커버리지 경고](#6-bm25-커버리지-경고)
7. [SQLite 버전 요구사항](#7-sqlite-버전-요구사항)
8. [임베딩 백엔드 (EMBEDDING_BACKEND)](#8-임베딩-백엔드-embedding_backend)

---

## 1. 스토어 구조

LocalCrab은 `STORAGE_MODE` 환경변수로 네 가지 백엔드를 선택한다: `local`(기본),
`kuzu`(그래프 capability-negative, 나머지는 local과 동일), `docker`, `pg`(4스토어
전부 PostgreSQL 통합).

| 스토어 역할 | local 모드 | kuzu 모드 | pg 모드 | docker 모드 |
| --- | --- | --- | --- | --- |
| 그래프 | `LocalGraphStore` (`graph.db`, SQLite) | `KuzuUnavailableGraphStore` (파일 미생성) | `PGGraphStore` (공유 SQLAlchemy 엔진) | `Neo4jStore` (`bolt://localhost:7687`) |
| 문서 | `LocalSQLDocStore` (`doc_store.db`, SQLite) | `LocalSQLDocStore` (local과 동일) | `PgDocStore` (공유 엔진) | `MongoStore` (MongoDB) |
| 벡터 | `SqliteVecStore` (`vectors.db`, 기본) / `ChromaStore` (PersistentClient, `chroma/`, 옵션) | local과 동일 | `PgVectorStore` (공유 엔진, HNSW) | `ChromaStore` (HttpClient) |
| SQL | `SQLStore` (`opencrab.db`, SQLite) | `SQLStore` (local과 동일) | `SQLStore` (`POSTGRES_URL`, 자체 엔진) | `SQLStore` (PostgreSQL) |

`kuzu` 모드는 `is_local=True`이며 문서·벡터·SQL 스토어만 local과 동일하게 선택된다.
그래프는 `KuzuUnavailableGraphStore`를 반환한다. Ladybug의 트랜잭션 소유권과
원자적 CAS를 검증하기 전까지 production constructor는 optional 패키지를 import
하거나 경로를 만들지 않고 capability 예외를 낸다. qualification bundle은
`python3 -m tests.kuzu_qualification`으로 읽기 전용 검사한다.

`pg` 모드는 `is_local=False`(별도 분기 — `local`/`kuzu`의 SQLite 변형이 아니다).
graph/vector/doc 3스토어는 factory가 `POSTGRES_URL`당 1회 생성해 캐시하는 **공유
SQLAlchemy 엔진**(`_get_pg_engine`, 단일 커넥션 풀)을 주입받는다. `SQLStore`만
기존 시그니처(`url` 인자)를 유지하기 위해 자체 엔진을 연다(같은 DB를 향하지만
별도 풀). 설치: `pip install ".[pg]"`. 설계·프리플라이트 실측:
`docs/pgvector-migration-plan.md` (B) 경로.

**운영 권장 구성**: 기본은 `local` — 4스토어(graph/doc/sql/vector)를 SQLite 단일
규율로 통일해 백업(디렉터리 1개 파일 복사)·정합성 관리 대상을 1개로 줄인다.
실시간 동시 write(MCP 서빙 중 백그라운드 로더)가 확정 요구이거나 벡터가 수백만
스케일이면 `docker` 4종 혼합이 아니라 **`pg` 모드(PostgreSQL 단일 통합)**로
이행하는 편이 낫다 — MVCC로 리더가 라이터를 막지 않고, graph/vector/doc가 단일
커넥션 풀을 공유한다. `docker` 모드는 Neo4j/MongoDB/PostgreSQL/Chroma 4종 외부
서비스를 각각 백업·버전관리·정합성 관리해야 하는데, SaaS 규모(다중 테넌트,
조직 단위 격리, 팀별 별도 인프라 요구)가 아니면 이 관리 비용이 Neo4j/Mongo
개별 이점을 상회한다.

벡터 백엔드는 `VECTOR_BACKEND`(미설정 시 `STORAGE_MODE`·`EMBEDDING_BACKEND` 조건부 결정)로 선택한다. 상세 규칙·매트릭스는 §8과 `docs/vector-backends.md` 참고.

### 팩토리 (`opencrab/stores/factory.py`)

```
_get_pg_engine(url)   # lru_cache(maxsize=8) — 1 Engine per POSTGRES_URL, shared by
                       # graph/vector/doc below (single connection pool, §3.5)

make_graph_store(settings)
    STORAGE_MODE=pg   → PGGraphStore(_get_pg_engine(POSTGRES_URL))
    STORAGE_MODE=kuzu → KuzuUnavailableGraphStore()  # graph capability-negative
    is_local          → LocalGraphStore(db_path="<LOCAL_DATA_DIR>/graph.db")
    else              → Neo4jStore(uri=NEO4J_URI, ...)

make_doc_store(settings)
    is_local            → LocalSQLDocStore(db_path="<LOCAL_DATA_DIR>/doc_store.db")
    STORAGE_MODE=pg     → PgDocStore(_get_pg_engine(POSTGRES_URL))
    else                → MongoStore(uri=MONGODB_URI, db_name=MONGODB_DB)

make_vector_store(settings)
    VECTOR_BACKEND(명시 또는 조건부 기본, STORAGE_MODE=pg → "pgvector") 로 분기:
      "sqlite-vec" → SqliteVecStore(db_path="<LOCAL_DATA_DIR>/<VECTOR_DB_FILE>")  # EMBEDDING_BACKEND=local 조합 시 ValueError
      "pgvector"   → PgVectorStore(engine or POSTGRES_URL, dim=EMBED_DIM,        # EMBEDDING_BACKEND=local 조합 시 ValueError
                                    collection=EMBED_COLLECTION, ef_search=PG_EF_SEARCH)
                     # STORAGE_MODE=pg → 공유 엔진 주입, 그 외(VECTOR_BACKEND=pgvector 명시)는 자체 엔진
      "chroma"     → ChromaStore(local_mode=is_local, local_path="<LOCAL_DATA_DIR>/chroma")

make_sql_store(settings)
    STORAGE_MODE in (docker, pg) → SQLStore(url=POSTGRES_URL)   # own engine, same DB as pg's shared pool
    else                         → SQLStore(url="sqlite:///<LOCAL_DATA_DIR>/opencrab.db")
```

`LOCAL_DATA_DIR` 기본값: `~/.local/share/localcrab` (실행 사용자의 HOME 에서
파생된다 — `config.py::_default_local_data_dir`, 회귀 방지는
`tests/test_config_defaults.py`).

---

## 2. LocalSQLDocStore 선택 근거

### 문제: JSON 파일의 O(N) 전체 로드

`LocalDocStore`는 모든 읽기·쓰기에서 JSON 파일 전체를 메모리에 올린다.

- `_load()`: 전체 파일 역직렬화 — O(N)
- `_save()`: 전체 dict 직렬화 후 atomic rename — O(N)
- `list_nodes(limit=50000)`: 전체 파일 로드 후 슬라이스 — O(N)

### 핫 패스: BM25 캐시 재구성 (2026-06 개선 — 백그라운드 재빌드 + 경량 fingerprint)

`BM25Index` 캐시는 원래 설계 의도("쓰기 시에만 재빌드, 쿼리마다 하지 않음", 커밋
`ab21c95`)대로 동작하도록 정리됐다. 과거 회귀로 (a) dirty면 다음 쿼리가 동기
재빌드를 떠안고 (b) not-dirty여도 매 쿼리 `list_nodes(50000)`+`json.loads`로
fingerprint를 확인하던 비용이 쿼리 hot path에 실려 있었다(코퍼스 ~16만 노드 기준
부하 큼). 현재는:

- **재빌드는 백그라운드 워커**가 수행한다. `invalidate_bm25_cache()`(쓰기 핸들러가
  호출)는 세대 카운터만 bump하고 워커를 깨운다(디바운스 `OPENCRAB_BM25_DEBOUNCE`,
  기본 1.5s — 연속 ingest를 1회로 합침). 완료되면 새 인덱스를 **원자적 참조 교체**로
  swap-in한다(단일 프로세스/GIL). 쿼리는 그동안 기존(약간 stale) 인덱스를 즉시 서빙.
- **쿼리 hot path는 경량 fingerprint만** 확인한다 — `doc_store.bm25_fingerprint()`
  = `SELECT COUNT(*), MAX(updated_at) FROM (SELECT … LIMIT N)`(행 파싱 없음,
  `idx_doc_nodes_updated` 활용). 별도 프로세스(팩 적재·reingest)가 `invalidate`
  없이 doc_nodes에 쓴 out-of-band 변경을 이 probe가 잡아 백그라운드 재빌드를
  스케줄한다. `LIMIT N`은 `_BM25_NODE_LIMIT`과 일치시켜, 코퍼스가 N을 넘어도 count가
  `BM25Index`(N개만 색인)와 어긋나지 않게 한다.
- 유일한 동기 빌드는 **콜드 스타트**(캐시 없음)뿐.

```python
# opencrab/ontology/query.py
_BM25_NODE_LIMIT = int(os.getenv("OPENCRAB_BM25_NODE_LIMIT", "50000"))

# 쿼리 hot path: 경량 probe만 (불일치 시 백그라운드 재빌드 예약, stale 서빙)
fp = self._doc_store.bm25_fingerprint(limit=_BM25_NODE_LIMIT)
if fp != self._bm25_cache.fingerprint:
    self.invalidate_bm25_cache()  # → 백그라운드 워커가 재빌드 후 atomic swap
```

검색 품질(relevance·토크나이저·스코어링·커버리지)은 무변경 — `BM25Index.build/search`
로직을 건드리지 않는다. freshness만 write-triggered(디바운스 창에서만 stale).

> **키워드 FTS 레그(2026-06):** BM25는 그래프 **노드 필드**를 색인하므로 청크 **본문**
> 속 약어·표준번호(JASO M345, FB/FC)는 놓칠 수 있다. 이를 보완해 `HybridQuery`는
> 벡터·BM25·그래프에 더해 **키워드 레그**(`_fts_search`)를 추가한다 — doc store가
> `supports_keyword` capability를 노출할 때만 `keyword_search(...)`를 호출(미지원 시 폴백).
> `LocalSQLDocStore`는 `doc_sources` 본문을 SQLite **FTS5**(`doc_sources_fts`,
> `unicode61` 한+영)로 색인한다. 다른 백엔드(Mongo/pgvector)는 동일 capability로 구현.
> 자세한 내용·한계는 `docs/pgvector-migration-plan.md` §7.1.

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
| `upsert_node_doc` | O(N): 전체 재직렬화 | O(log N): `INSERT ... ON CONFLICT DO UPDATE` |
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

`properties` / `metadata` / `details`는 JSON TEXT로 저장한다. **컬럼 전체를 왕복할
때는** 파싱을 SQL이 아니라 Python `json.loads()`로 처리한다. 다만 한 키만 보는
필터·스코프 질의(`keyword_search()`의 space·pack 필터 등)는 SQL에서 `json_extract()`를
쓴다. 그 가용성은 버전이 아니라 빌드 옵션에 달려 있다(§7 참조).

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

## 4. Kuzu(ladybug) 그래프 스토어 - capability 상태

현재 `STORAGE_MODE=kuzu`는 operational backend가 아니다. `factory.py`는
`KuzuUnavailableGraphStore`를 반환하며 그래프 DB 경로, optional driver import,
DDL, DML을 건드리지 않는다. 그래프 mutation과 read query는 capability 예외를
낸다. Ladybug transaction owner와 node/edge 원자적 CAS를 검증한 qualification
증거를 추가하기 전까지 production constructor를 활성화하지 않는다.

qualification bundle은 `tests/fixtures/issue80/qualification/`에 있으며
`python3 -m tests.kuzu_qualification` 명령으로 패키지를 import하지 않고 무결성을
검사한다. raw graph fixture setup은 전용 marker가 있는 OS 임시 디렉터리에서만
`tests/helpers/issue80_graph_mutation.py`가 수행한다.

### 4.1 전역 노드 ID 이관

SQL graph의 정본 키는 `node_id` 하나다. 기존 `(node_type, node_id)` 스키마는
`inspect_graph_identity()`로 모든 typed node와 edge를 읽어 source fingerprint를
계산한다. `migrate_graph_identity()`의 기본 경로는 read-only dry-run이며
canonical plan bytes를 반환한다. 운영자가 `scripts/migrate_graph_identity.py
--apply`를 명시적으로 호출할 때만 저장된 plan bytes, plan SHA-256, backup SHA-256,
request ID를 함께 검증하고 SQLite는 `BEGIN EXCLUSIVE`, PostgreSQL은 advisory lock
안에서 staging과 cutover를 수행한다.

dry-run 매핑 파일(`--mapping-file`)은 JSON 객체이며 `mappings`와
`property_resolutions` 두 리스트 멤버는 모두 선택이다. legacy 노드는 `node_type`과
`node_id`만 담은 중첩 `source` 객체로 지목한다. `mappings` 안에서는 그 노드의 digest를
형제 필드 `source_digest`로 함께 적어서, `rename` 항목은 `source`와 `source_digest`를
직접 갖고 `merge` 항목은 `sources` 리스트의 각 원소가 같은 두 필드를 갖는다.
`property_resolutions` 항목의 `source`는 같은 2키 객체지만 digest를 요구하지 않는다.
receipt가 `mapping_result`의 각 매핑 아래 보고하는 source는 세 필드를 평탄화한 별개
모양이므로 매핑 파일에 그대로 옮겨 쓸 수 없다. 형식 전문과 예시는 `scripts/migrate_graph_identity.py`의 모듈 docstring에 있다.

dry-run은 graph와 migration ledger를 변경하지 않는다. apply는 성공한 receipt를
ledger에 한 번만 기록하며 같은 request ID의 재실행은 저장된 receipt를 그대로
반환한다. 이미 target 스키마인 graph가 plan과 canonical digest까지 일치하면
table 교체 없이 receipt만 기록하고, 다른 상태는 target conflict로 중단한다.
Neo4j와 Kuzu에는 이 SQL migration apply capability를 노출하지 않는다.

아래의 기존 Phase 2 기록은 현재 동작을 설명하지 않는 보존 문서다. 현재 지원하는
production 그래프 경로는 `local`, `pg`, `docker`이며 모두 global `node_id`,
canonical digest, edge key, writer transaction 규율을 따른다. Neo4j는 기동할 때
OpenCrabNode 관계의 endpoint/type, edge key, digest를 재검증하며 중복이나 불일치가
있으면 `partial_or_unknown` 상태로 쓰기를 차단한다.

> 다음 내용은 capability-negative 전환 전의 보존된 historical 기록이다. 현재
> `STORAGE_MODE=kuzu`는 그래프를 초기화하지 않으며 production 경로가 아니다.
> 아래의 구현 완료, 실행 가능, 동작한다는 표현은 당시 코드의 기록일 뿐 현재
> 지원 상태를 나타내지 않는다.

### Phase 전략 (현황 반영)

```
Phase 1 (완료): Neo4j → LocalGraphStore (SQLite BFS)
    목표: Docker 없이 로컬 실행 가능, 안정성 최우선
    결과: MCP 도구 전체 로컬 동작 확보

Phase 2 (보류, capability qualification 전): LocalGraphStore → KuzuGraphStore
    (런타임 패키지 ladybug>=0.18, STORAGE_MODE=kuzu)
    historical 결과: run_cypher()는 KuzuGraphStore에서 실동작(ladybug Database/
    Connection API 사용)하지만, 상위 코드(query.py/impact.py/neo4j_export.py/
    mcp/tools.py)는 여전히 `isinstance(x, (LocalGraphStore, KuzuGraphStore))`로
    두 스토어를 묶어 동일한 우회 메서드 경로(find_by_relations/list_packs/
    export_nodes·edges/get_node_by_id, Python BFS)를 탄다 — Cypher 가변 관계
    패턴(`-[*1..N]-`)으로의 전환이나 isinstance 분기 제거는 아직 이뤄지지
    않았다. KuzuGraphStore는 이 우회 메서드들을 자체적으로(내부적으로 Cypher를
    쓰기도 하지만) 재구현했을 뿐, 호출부는 LocalGraphStore와 동일하게 취급한다.
```

과거 RPi5 16KB 페이지 커널에서 구버전 kuzu(0.11.3)의 buffer manager가 4KB 단위
`madvise` 호출로 EINVAL이 발생해 죽던 문제(`LD_PRELOAD=madv_noop.so` 우회 필요,
`LadybugDB/ladybug#526`)는 `#527`("Handle larger OS page sizes in VM eviction")로
수정되어 **ladybug v0.18.0(2026-07-01)부터 우회 없이 동작**한다. 이 브랜치에서
`madv_noop.so`/`LD_PRELOAD` 워크어라운드는 factory.py/kuzu_graph_store.py/
migrate_graph_to_ladybug.py의 히스토리 주석으로만 남아 있고, 현재 빌드/실행
경로 어디에서도 요구되지 않는다. `scripts/madv_noop.c` 파일 자체는 저장소에
남아 있으나 Makefile 등 어떤 빌드 설정에서도 더 이상 참조되지 않는 죽은
코드다. 설치: `pip install ".[kuzu]"` (`ladybug>=0.18`). 기존 `graph.db`
(SQLite) → `.kuzu` 마이그레이션은 `scripts/migrate_graph_to_ladybug.py`.

### 보존 기록: 당시 LocalGraphStore의 한계

`LocalGraphStore.run_cypher()`는 영구 no-op이었다 (`local_graph_store.py:298`). 다음
내용은 capability-negative 전환 전의 우회 경로 기록이다:

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
| `ReBACEngine.check()` | `find_neighbors(depth=1,2)` — Python BFS direct/transitive |
| `keyword_search` | `export_nodes() + Python str.lower() 포함 검사` |
| 엣지 저장 시 노드 타입 조회 | `get_node_by_id()` |

(구 `ontology_rebac_check` MCP 툴은 실사용 이력 0으로 삭제됨(Stage 7) — 위 행은
`query.py`의 policy-aware 필터링이 여전히 호출하는 내부 엔진 `ReBACEngine.check()`
기준으로 갱신했다.)

`ReBACEngine.check()` 의 실패 계약(#78): SQL 정책 조회가 예외를 내거나 `bool | None`
밖 값을 돌려주면 그래프 탐색 없이 deny 를 돌려주고 예외를 전파하지 않는다. 읽지 못한
명시 DENY 행을 그래프 GRANT 가 덮어쓰지 않게 하기 위해서다. WARNING 은 예외 타입명과
식별자만 담고 원문은 DEBUG 에만 남긴다. 그래프 경로는 종전대로 예외를 삼키고 DEBUG 로
남긴다. 재현: `pytest tests/test_rebac_local.py -k TestSQLStoreFailure`.

당시 코드베이스 전반에는 `isinstance(graph, (LocalGraphStore, KuzuGraphStore))` 분기가
존재한다(`ontology/query.py`, `ontology/impact.py`, `pack/neo4j_export.py`,
`mcp/tools.py`) — `kuzu` 모드 도입 시 이 분기에서 `LocalGraphStore`를
`KuzuGraphStore`로 **교체**한 것이 아니라 **튜플에 추가**했다. 즉 `KuzuGraphStore`도
Neo4j 취급이 아니라 이 우회 경로를 그대로 탄다(위 표의 우회 메서드들을
`KuzuGraphStore`가 자체 구현). 또한
`find_neighbors()`는 Cypher 가변 관계 패턴(`*1..N`) 대신 Python BFS로 구현되어 있어,
허브 노드(차수 수백 이상)에서 성능 열화가 발생한다 (`bench_graph_backends.py`
실측, `LocalGraphStore` 기준: 43k 노드 / 최고 차수 615에서 d1 p50 = 11.86ms, 20k
대비 32× 급등). `KuzuGraphStore.find_neighbors()`도 depth=1은 전용 1-hop 쿼리,
depth>1은 동일한 Python BFS 큐 방식이라(`kuzu_graph_store.py`) Cypher 가변 길이
패턴으로의 교체는 `kuzu` 모드에서도 아직 이뤄지지 않았다.

> **BFS SQL LIMIT 최적화**: `find_neighbors()`는 각 탐색 스텝에서 `remaining = limit - len(results)`를
> SQL LIMIT에 전달해 fetchall I/O 자체를 줄이고, 내부 루프에서도 limit 도달 시
> 즉시 break한다. SQL LIMIT만으로는 pack 필터 통과율이 낮을 때 보완이 안 되므로
> 두 guard를 병용한다.

### 보존 기록: ladybug(kuzu 모드) 도입 당시의 주장

다음 목록은 capability-negative 전환 전에 작성된 기록이다. 현재 production
경로에서 달성된 기능으로 해석하면 안 된다.

- `pip install ".[kuzu]"`로 설치 가능한 임베디드 컬럼형 그래프 DB(KùzuDB 리브랜딩,
  https://github.com/LadybugDB/ladybug). `Database`/`Connection` API는 kuzu와 동일.
- 당시 `KuzuGraphStore.run_cypher()`가 **실제로 Cypher를 실행**했다는 기록이 있다 —
  `local` 모드의 `LocalGraphStore.run_cypher()`는 여전히 영구 no-op.
- RPi5 16KB 페이지 커널 madvise 크래시가 업스트림에서 해결되어(`#527`)
  `LD_PRELOAD=madv_noop.so` 우회가 더 이상 필요 없다.
- 당시 `scripts/migrate_graph_to_ladybug.py`로 기존 `graph.db`(SQLite) →
  `graph.kuzu` 마이그레이션을 시도할 수 있었다는 기록이 있다. 현재 apply는
  fixture-only이다.

아직 실현되지 않은 것 (당초 "기대 효과"로 적혀 있었으나 현재도 유효한 갭):

- `list_packs`, `find_by_relations`, `export_nodes/edges`, `get_node_by_id`가
  Cypher로 통합 대체되지 않았다 — `KuzuGraphStore`가 동일한 이름의 메서드를
  자체적으로 다시 구현했을 뿐, 호출부(ontology/query.py 등)는 `run_cypher()`를
  직접 쓰지 않는다.
- `isinstance(graph, (LocalGraphStore, KuzuGraphStore))` 분기는 제거되지 않고
  오히려 튜플로 확장되었다.
- `find_neighbors()`의 Python BFS → Cypher 가변 관계 패턴(`-[*1..N]-`) 교체는
  `kuzu` 모드에서도 이뤄지지 않았다.
- `STORAGE_MODE` 기본값은 여전히 `local`이다 — `kuzu`는 opt-in이며 default
  승격 계획은 코드/설정 어디에도 명시돼 있지 않다.

### 검증 상태

- [x] ladybug의 `Database`/`Connection` API가 kuzu와 동일함을 확인(모듈
      docstring, `kuzu_graph_store.py` 헤더 주석) — 클래스명·`STORAGE_MODE="kuzu"`
      값은 하위호환을 위해 그대로 유지.
- [x] RPi5 16KB 페이지 커널 madvise 크래시 수정 확인(`LadybugDB/ladybug#526`→`#527`,
      v0.18.0에 포함) — 우회 불필요.
- [x] 당시 `graph.db`(SQLite) → ladybug 마이그레이션 스크립트가 작성됐다는 기록
      (`scripts/migrate_graph_to_ladybug.py`). 현재 apply는 fixture-only이다.
- [ ] `find_neighbors()` 결과 집합이 Neo4j 모드와 동등한지 검증 (Jaccard 유사도 기준) — 미확인.
- [ ] MCP 서버 멀티스레드 환경에서 임베디드 DB의 동시 접근 안전성 검증 — 미확인.
- [ ] 대규모 그래프(430k+ 노드)에서 ladybug 인덱스 빌드 시간 측정 — 미확인.
- [ ] `list_packs`, `export_nodes/edges`, `find_by_relations`를 Cypher 로직으로
      통합해 우회 메서드 중복을 제거할지 여부 — 설계 미착수.

---

## 5. 마이그레이션 절차

### Docker → local 전환 (자동)

`scripts/migrate_to_local.py`를 사용하면 Neo4j + MongoDB + HTTP Chroma + PostgreSQL의
데이터를 로컬 SQLite/Chroma로 원클릭 전환할 수 있다.

```bash
# Dry-run: 소스 서비스 연결 확인 + 데이터 규모 보고 (쓰기 없음)
uv run python scripts/migrate_to_local.py --dry-run

# 전체 마이그레이션 (기존 로컬 파일 자동 백업 후 진행)
uv run python scripts/migrate_to_local.py

# 특정 단계만 실행
uv run python scripts/migrate_to_local.py --skip-vectors --skip-sql
```

6단계 파이프라인:
1. Pre-flight: 소스 서비스 연결 + 데이터 규모 확인 (READ ONLY)
2. Backup: 기존 로컬 DB 파일 타임스탬프 접미사 백업 (`graph.db.bak.YYYYMMDD_HHMMSS`)
3. Graph: Neo4j → LocalGraphStore (SKIP/LIMIT 페이징, `upsert_nodes_batch`)
4. Docs: MongoDB → LocalSQLDocStore (nodes / sources / audit_log)
5. Vectors: HTTP Chroma → PersistentClient (임베딩 재계산 없이 원본 복사)
6. SQL: PostgreSQL → SQLite (INSERT OR IGNORE, `result.rowcount` 기반 정확 카운트)

### 수동 전환

**1. 환경변수 전환**

```bash
export STORAGE_MODE=local
export LOCAL_DATA_DIR=/your/data/dir   # 기본: ~/.openclaw/workspace/data/localcrab
```

**2. 데이터 디렉토리 구조**

```
<LOCAL_DATA_DIR>/
  graph.db          # LocalGraphStore (SQLite)
  graph.db-wal      # WAL 파일 — 백업 시 반드시 포함
  graph.db-shm      # 공유 메모리 파일 — 백업 시 반드시 포함
  doc_store.db      # LocalSQLDocStore (SQLite)
  chroma/           # Chroma PersistentClient
  opencrab.db       # SQLStore (SQLite) — ontology_nodes/edges, impact_records,
                     # lever_simulations, rebac_policies
  billing.db        # billing_events 전용 (issue #105부터 opencrab.db 분리 —
                     # write.lock 쓰기와 SQLite 파일 잠금이 경합하지 않도록)
```

> **issue #105 이전 설치라면 `opencrab.db`에도 `billing_events` 테이블이 남아
> 있을 수 있다.** 그 테이블은 의도적으로 손대지 않는다 — 이름도 안 바꾸고
> 복사도 하지 않는다(자동 마이그레이션을 시도했다가 코드 리뷰에서 원자성·
> 동시 기동·잠금 없는 스키마 쓰기 세 가지 결함이 나와 되돌렸다). 이 저장소
> 안에서 `BillingHooks.get_usage()`/`list_events()`를 부르는 코드가 없어
> (grep으로 확인, 테스트 제외 0건) 지금은 과거 이력이 두 파일에 나뉘어
> 있어도 실질적 영향이 없다. **나중에 이 데이터를 실제로 읽는 소비자가
> 생기면** 그때 `scripts/migrate_sqlite_to_pg.py`와 같은 형태의 1회성
> 스크립트를 작성해 `opencrab.db`의 구 `billing_events`를 `SELECT`로 읽어
> `billing.db`에 `INSERT OR IGNORE`하면 된다 — 사람이 직접 실행하는 단발성
> 작업이므로 기동 경로의 크래시 복구·동시성 설계가 필요 없다.

**3. 수동 백업**

WAL 모드 사용 시 `.db`만 복사하면 체크포인트되지 않은 WAL 데이터가 누락된다.
세 파일을 함께 복사해야 한다.

```bash
cp graph.db graph.db-wal graph.db-shm /backup/path/
cp doc_store.db /backup/path/
cp opencrab.db /backup/path/
cp billing.db /backup/path/
cp -r chroma/ /backup/path/chroma/
```

> `migrate_to_local.py`의 backup 단계는 `-wal`, `-shm` 파일과 `billing.db`를
> 자동으로 함께 복사한다.

**4. 검증**

```bash
opencrab status
opencrab manifest
opencrab query "test"
```

`status` 출력에서 `LocalGraphStore`, `LocalSQLDocStore`, `ChromaStore (local)` 가
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

### 최소 버전: SQLite 3.24.0 + JSON 함수 활성화 빌드

요구사항은 두 갈래이며 성격이 다르다.

**버전 하한 3.24.0 (2018-06-04)** — SQLite의 UPSERT 절에서 온다.
`INSERT ... ON CONFLICT (...) DO UPDATE SET`와 `... DO NOTHING`은 둘 다 3.24.0부터
지원된다. 이 저장소는 두 형태를 모두 쓰며, `SqlDialect.upsert()` 헬퍼를 경유하는
경로와 헬퍼 없이 문을 직접 조립하는 경로가 함께 있다. **어느 쪽이든 하한은 같다** —
같은 문법이기 때문이다. 코어 문법이라 버전 숫자만으로 가용성이 보장된다.

**JSON 함수 활성화** — `json_extract()`는 버전으로 보장되지 않는다. JSON1과 함께
3.9.0(2015-10-14)에 도입됐지만 3.37.2까지는 빌드 옵션(`SQLITE_ENABLE_JSON1`)이었고,
3.38.0부터 기본 포함이지만 여전히 `SQLITE_OMIT_JSON`으로 제외할 수 있다. 따라서
버전이 아니라 함수 자체를 확인해야 한다. (3.38.0부터 추가된 `->` / `->>` **연산자**는
PostgreSQL 분기에서만 쓰이므로 SQLite 경로의 요구사항이 아니다.)

`json_extract()`는 **graph store와 doc store 양쪽**이 필터·스코프 질의에서 쓴다.
대표 예는 다음 둘이다(전수 목록이 아니다).

```python
# 그래프: pack_id 함수 인덱스 DDL과 list_packs() / export_nodes()
"CREATE INDEX IF NOT EXISTS idx_nodes_pack"
" ON graph_nodes(json_extract(properties, '$.pack_id'))"

# 문서: keyword_search() 의 space·pack 필터 (SqlDialect.json_get /
#       json_truthy_text 경유, 위치 플레이스홀더 바인딩)
"... AND json_extract(s.metadata, '$.space') IN (?, ...) ..."
```

지금 어디서 쓰는지는 코드에 물어본다. 아래는 **텍스트 후보 검색**이라 docstring·주석·
부정문("`OR REPLACE`를 쓰지 않는다" 같은 서술)까지 잡는다. 실행 사용처와 같지 않으니
결과를 걸러 읽어야 한다.

```bash
grep -rn "ON CONFLICT" --include="*.py" opencrab/ scripts/
grep -rn "json_get(\|json_truthy_text(" --include="*.py" opencrab/
```

### 확인

버전 출력만으로는 부족하므로 함수 실행까지 확인한다.

```bash
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
python3 -c 'import sqlite3; print(sqlite3.connect(":memory:").execute("SELECT json_extract(?, ?)", ("{\"pack_id\": \"p1\"}", "$.pack_id")).fetchone()[0])'   # p1 이 출력되면 JSON 함수가 있다
```

3.24.0 미만이면 upsert가 문법 오류로 실패한다. JSON 함수가 없으면 로컬 모드 초기화 시
인덱스 생성이 실패하고 `LocalGraphStore`가 `available=False`로 설정된다.

`LocalSQLDocStore`도 두 요구를 모두 진다. JSON 컬럼 **전체를 왕복할 때는** Python
`json.loads()`로 파싱하지만, `keyword_search()`의 필터와 공유 베이스의 스코프 술어는
SQL에서 `json_extract()`를 쓴다. upsert 경로 역시 그대로 타므로 3.24.0 하한도
적용된다.

---

## 8. 임베딩 백엔드 (EMBEDDING_BACKEND)

`EMBEDDING_BACKEND` 환경변수로 전환:
- `openai` (기본): OpenAI 호환 `/v1/embeddings` API 백엔드(*모델*이 아니라 *전송 방식*). 실제 OpenAI 클라우드 모델(`text-embedding-3-*`)도, 자체호스팅 서버(LM Studio·Ollama·vLLM·HF TEI) 모델도 사용 가능. 모델은 `OPENAI_EMBED_MODEL`, 차원은 `EMBED_DIM`으로 지정. 한국어 추천 기본은 KURE-v1 (한국어 SOTA, 1024d). 컬렉션: `opencrab_vectors_kure`. primary(원격 서버) 실패 시 로컬 GGUF로 자동 폴백(KURE-v1-Q8_0, ~635MB, 자동 다운로드, `pip install "opencrab[gguf]"`로 `llama-cpp-python` 설치 필요; 저사양은 `LOCAL_GGUF_PATH`로 Q4_K_M 지정 가능) — 외부 서버 없이도 완전 로컬 동작 가능.
  - **경량 대안(CPU 부담 시)**: [`BM-K/KoSimCSE-roberta`](https://huggingface.co/BM-K/KoSimCSE-roberta) (RoBERTa-base, ~110M, 768d) — KURE보다 가볍지만 한국어 전용·품질 다소 낮음. OpenAI 호환 서버(HF TEI 등)에 서빙 + `EMBED_DIM=768` + 별도 `EMBED_COLLECTION`이면 코드 수정 없이 사용(전량 재색인). 로컬 GGUF 폴백은 GGUF 빌드 필요해 기본 미적용.
  - **한 컬렉션 = 한 모델**: 모델·차원을 바꾸면 새 `EMBED_COLLECTION` + 전량 재색인 필요. 서로 다른 모델 벡터를 한 컬렉션에 섞지 말 것. primary/fallback도 동일 모델·차원이어야 함.
- `local` (롤백 옵션): ChromaDB 기본 EF (all-MiniLM-L6-v2, ONNX, 384d). 컬렉션: `opencrab_vectors`. 설정 없이 바로 동작하지만 한국어 검색 품질이 낮다. `VECTOR_BACKEND=sqlite-vec`와는 조합 불가(기동 시 ValueError) — sqlite-vec를 쓰려면 `EMBEDDING_BACKEND=openai`가 필요.

**KURE 아키텍처**:
```
make_vector_store(settings)
  └─ EMBEDDING_BACKEND=openai
       └─ ChromaStore("opencrab_vectors_kure", ef=ResilientEmbeddingFunction)
            ├─ primary[0]: OpenAIEmbeddingFunction (GPU, http://embed-host-1:1234/v1)
            ├─ primary[1]: OpenAIEmbeddingFunction (GPU, http://embed-host-2:1234/v1)  ← 선택
            ├─ ...                                    (OPENAI_API_BASE 콤마 구분 순서)
            └─ fallback: LlamaCppEmbeddingFunction (RPi5 CPU, 로컬 GGUF Q8_0)
```

- **단일 컬렉션**: 적재·검색·폴백 모두 동일 KURE-v1 가중치(Q8_0) → 벡터 완전 호환.
- **다중 엔드포인트 순차 체인**: `OPENAI_API_BASE`에 콤마로 여러 URL을 지정하면 primary가
  리스트가 되어 순서대로 시도된다. 첫 원격이 죽어도 GGUF(CPU, 느림)로 내려가기 전에 다음
  원격을 우선 시도. 단일 URL이면 길이 1 체인으로 기존 동작과 100% 동일. 모든 엔드포인트는
  동일 모델(KURE-v1)을 서빙한다고 가정한다(`name()`은 첫 primary 기준 → 컬렉션 재사용 보장).
- **자동 폴백 + 엔드포인트별 독립 TTL**: 각 primary는 실패 시 15초(health_ttl) 동안 개별적으로
  건너뛴다 — 죽어 있는 엔드포인트 하나가 다음 엔드포인트 시도까지 지연시키지 않는다. 모든
  primary가 실패/unhealthy면 로컬 GGUF 폴백. 복구 후 TTL 만료 시 자동 복귀(`force_check()`로
  즉시 해제 가능).
- **GGUF 자동 다운로드**: `LOCAL_GGUF_PATH` 미설정·파일 없으면 HuggingFace에서 자동 다운로드.
- **컬렉션 분리**: minilm(384d)과 KURE(1024d)는 차원이 달라 별도 컬렉션 유지. 롤백 즉시 가능.

**벡터 스토어 백엔드 (`VECTOR_BACKEND`) — 임베딩과 독립 축**:
`make_vector_store` 는 먼저 `VECTOR_BACKEND` 로 분기한다(`EMBEDDING_BACKEND` 분기는 `chroma` 내부).
`VECTOR_BACKEND` 미설정 시 조건부 기본(`vector_backend_resolved`, 체크 순서 고정): ①
`STORAGE_MODE=pg` → `pgvector`; ② (아니고) `STORAGE_MODE=local/kuzu` + `EMBEDDING_BACKEND=openai`(기본) →
`sqlite-vec`; ③ 그 외(`STORAGE_MODE=docker` 이거나 `EMBEDDING_BACKEND=local`) → `chroma`. 명시 설정은 항상 우선.
- `sqlite-vec`(로컬 모드 기본): `SqliteVecStore`(vec0, `LOCAL_DATA_DIR/vectors.db`). 벡터를 graph/doc/sql 과 같은
  SQLite WAL 규율에 편입 → Chroma 다중프로세스 쓰기 제약/flock 층 제거. 임베딩은 KURE 공유 헬퍼
  `_make_kure_embedding_function` 로 앱측 계산 후 INSERT. `EMBEDDING_BACKEND=local`과 조합 시 ValueError.
  검색 경로는 이원화: pack-scoped 는 partition key exact KNN(~8ms), 전역(pack 미지정)은 기본 exact
  브루트포스이며 `VECTOR_ANN=binary` 옵트인 시 **binary 2단계 ANN**(bit hamming coarse
  `VECTOR_ANN_COARSE_K`개 → float cosine rerank)으로 가속(기본 off, 기존 경로 불변; 기존 DB 는
  `scripts/migrate_add_binary_quantization.py` 로 bit 컬럼 backfill).
  전환: `scripts/migrate_chroma_to_sqlite_vec.py`, 설계·성능(전역 브루트포스·binary 2단계):
  `docs/pgvector-migration-plan.md` (A) §3.6/§3.7, `docs/vector-backends.md` §4.1.
- `chroma`(docker 모드 기본 / local+minilm 조합 기본): `ChromaStore` (위 그림 그대로).
- `pgvector`(`STORAGE_MODE=pg` 기본): `PgVectorStore`(HNSW `m=16,ef_construction=64`,
  쿼리 세션 `hnsw.ef_search=PG_EF_SEARCH` 기본 500 — §4.2 Phase 2 게이트 재측정으로
  150→500 상향). `EMBEDDING_BACKEND=local`과 조합 시 ValueError(sqlite-vec와 동일 가드).
  전역 검색도 HNSW로 179,784건 전량 실측 p95 24.61ms라 sqlite-vec의 binary 2단계 같은
  별도 가속 불필요. 설계·실측: `docs/pgvector-migration-plan.md` (B) 경로,
  `docs/vector-backends.md` §4.2.

모드×옵션 전체 매트릭스는 `docs/vector-backends.md` 참고.

관련 파일:
- `opencrab/stores/openai_embedding.py` — OpenAI 호환 임베딩 EF
- `opencrab/stores/llamacpp_embedding.py` — 로컬 GGUF EF (자동 다운로드 포함)
- `opencrab/stores/resilient_embedding.py` — 다중 primary 순차 체인 + 폴백 자동 전환 래퍼
- `opencrab/stores/factory.py` — `make_vector_store` 백엔드 분기 (VECTOR_BACKEND × EMBEDDING_BACKEND)
- `opencrab/stores/sqlite_vec_store.py` — sqlite-vec(vec0) 벡터 스토어
- `opencrab/config.py` — `vector_backend`/`vector_db_file`/`vector_collection`, `embedding_backend`, `openai_*`, `local_gguf_path` 등

성능 비교 (실측):

| 모델 | top-1 | MRR | 정답-무관 마진 | 건당 속도 |
|------|-------|-----|----------------|-----------|
| minilm (기존, 384d ONNX) | 0/5 | 0.285 | -0.086 (무관↑) | 0.25s 로컬 |
| KURE-v1 LM Studio (주력, 1024d) | 5/5 | 1.000 | +0.447 | 0.06s GPU |
| KURE-v1 로컬 GGUF (폴백, 1024d) | 5/5 | 1.000 | +0.446 | 1.07s CPU |

벡터 일치도(LM Studio↔로컬 GGUF): cosine 평균 0.999853 — 폴백 호환 입증.
