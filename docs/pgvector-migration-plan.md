# PostgreSQL + pgvector 통합 마이그레이션 플랜

> 상태: 설계 문서(Design only). 코드 구현 없음.
> 선행 플랜 상호 참조: `[[ingestion-via-mcp-plan]]` (동시성의 1단계 — MCP 단일 라이터 경유 적재).
> 본 문서는 그 다음 단계인 **스토어 통합** 옵션이다.

---

## 0. 현황 요약 (코드 기준 정확 인용)

로컬 모드(`STORAGE_MODE=local`, `Settings.is_local == True`)의 4개 스토어:

| 역할 | 구현 클래스 | 백엔드(로컬) | 동시 쓰기 |
|------|-------------|--------------|-----------|
| graph | `LocalGraphStore` | SQLite `graph.db`, BFS는 파이썬 | SQLite WAL |
| doc | `LocalSQLDocStore` (`opencrab/stores/local_sql_doc_store.py`) | SQLite `doc_store.db` (`doc_nodes`/`doc_sources`/`audit_log`) | SQLite WAL |
| sql | `SQLStore` (`opencrab/stores/sql_store.py`) | SQLite `opencrab.db` | SQLite WAL |
| vector | `ChromaStore` (`opencrab/stores/chroma_store.py`) | Chroma `PersistentClient` | **단일 프로세스만** |

핵심 사실:

- **벡터 백엔드는 항상 Chroma다.** 로컬은 `PersistentClient`(`local_mode=True`), docker는 `HttpClient`. `make_vector_store` 가 두 경우 모두 `ChromaStore` 를 반환한다.
- **`SQLStore` 는 이미 PostgreSQL을 지원한다.** `__init__(url)`(`sql_store.py:142`) 에서 `url.startswith("sqlite")` 로 분기하며 SERIAL/TIMESTAMPTZ DDL(`_TABLES_SQL:20-79` — `ontology_nodes`/`ontology_edges`/`impact_records`/`lever_simulations`/`rebac_policies`)과 SQLite DDL(`_TABLES_SQL_SQLITE`)을 모두 갖고 있다. 설정값은 `config.py` 의 `postgres_url`(alias `POSTGRES_URL`, 기본 `postgresql://opencrab:opencrab@localhost:5432/opencrab`). 현재 `make_sql_store` 는 `settings.sqlite_url if settings.is_local else settings.postgres_url` 로 로컬에서 SQLite를 강제한다.
  - **드라이버 정정:** `SQLStore` 는 psycopg 를 직접 쓰지 않고 **SQLAlchemy `create_engine(url)`**(`sql_store.py:155-161`) 위에서 동작하며 `engine.begin()` 으로 트랜잭션을 연다. 따라서 신규 PG 스토어(vector/doc/graph)를 **같은 SQLAlchemy 엔진 인스턴스를 주입**받게 설계하면 4개 스토어가 **단일 커넥션 풀**을 공유한다 — 이것이 "단일 DB" 의 가장 깔끔한 실현이다(아래 §3·§5).
- **임베딩은 ChromaStore가 자동 수행한다.** `ChromaStore` 는 컬렉션 생성 시 `embedding_function=self._embedding_function` 를 받고, `add_texts`/`upsert_texts`/`query` 는 **벡터가 아닌 텍스트**를 그대로 넘긴다(`self._collection.add(documents=texts, ...)`, `query_texts=[query_text]`). 즉 임베딩 계산은 Chroma 내부에서 일어난다.
  - `embedding_function=None` → Chroma 기본 EF(all-MiniLM-L6-v2 ONNX, **384d**, `EMBEDDING_BACKEND=local`).
  - `ResilientEmbeddingFunction(primary=OpenAIEmbeddingFunction, fallback=LlamaCppEmbeddingFunction)` 주입 → KURE-v1(`EMBED_DIM=1024`), `EMBEDDING_BACKEND=openai`, 컬렉션 `EMBED_COLLECTION`(기본 `opencrab_vectors_kure`).
- **환경:** RPi5 aarch64. ARCHITECTURE.md Phase 1 에서 "Docker 없이 로컬 실행"을 위해 Neo4j→SQLite 전환을 이미 완료함.
- **동시성 맥락:** Chroma `PersistentClient` 는 다중 프로세스 동시 쓰기가 불가능하다. SQLite는 WAL + busy_timeout 으로 다중 프로세스가 가능하다. 즉 현재 스택에서 동시성의 약한 고리는 **벡터(Chroma)** 다.

---

## 1. 동기 (Motivation)

1. **단일 서버 통합.** vectors + doc + sql 을 PostgreSQL 한 서버에 모은다. 운영 대상 프로세스/파일이 줄고(현재 SQLite 3개 파일 + Chroma 디렉터리), 백업·모니터링·접속이 일원화된다. 네이티브 설치이므로 Docker는 여전히 불필요.
2. **MVCC 동시성.** PostgreSQL은 행 단위 락 + 스냅샷 격리(MVCC)로 **리더는 라이터를 막지 않고**, 라이터는 행 단위로만 경합한다. 다중 프로세스(MCP 서버 + 백그라운드 로더)가 락 충돌 없이 동시에 읽고 쓸 수 있다.
3. **표준 백업.** `pg_dump`/`pg_restore`/PITR 로 vectors·doc·sql 을 한 번에 정합성 있게 백업·복구. 현재는 SQLite 파일 복사 + Chroma 디렉터리 복사가 시점이 어긋날 수 있다.
4. **Chroma 단일프로세스 제약 해소.** 동시성의 약한 고리였던 벡터 쓰기를 MVCC로 해결한다. `[[ingestion-via-mcp-plan]]` 의 "단일 라이터 직렬화" 우회 없이도 다중 라이터가 가능해진다.
5. **단일 SQLAlchemy 엔진/풀 공유.** `SQLStore` 가 이미 SQLAlchemy 엔진을 쓰므로(§0), vector/doc/(graph) 스토어가 같은 엔진을 주입받으면 4개 스토어가 **하나의 커넥션 풀**로 동작한다. 프로세스당 커넥션 수가 줄고, 스토어 간 **교차 트랜잭션·조인**(예: 노드 등록과 벡터 INSERT 를 한 트랜잭션으로)이 가능해진다. 현행은 SQLite 3파일 + Chroma 가 각자 독립 핸들을 갖는다.

---

## 2. 아키텍처 비교표

| 항목 | 현행 (SQLite + Chroma) | 제안 (PostgreSQL + pgvector) |
|------|------------------------|------------------------------|
| 시스템 수 | SQLite 파일 3개(graph/doc/sql) + Chroma 디렉터리 | **Postgres 1개 서버 + 단일 SQLAlchemy 엔진/풀** (graph 통합 시 4스토어 전부 한 DB) |
| 벡터 동시 쓰기 | 단일 프로세스(Chroma 제약) | 다중 프로세스(MVCC) |
| doc/sql 동시 쓰기 | WAL 다중 프로세스(라이터 직렬화) | MVCC 행 단위 |
| 리더-라이터 | SQLite WAL: 리더 비차단 | MVCC: 리더 비차단 |
| 백업 | 파일 복사(시점 불일치 위험) | `pg_dump` 단일 정합 스냅샷 |
| RPi5 부담 | 매우 낮음(인프로세스) | 상시 서버 프로세스 + shared_buffers, HNSW 빌드 시 CPU/메모리 ↑ |
| 임베딩 위치 | Chroma 자동(minilm) / KURE explicit EF | **앱이 KURE EF 로 직접 계산 후 INSERT** (결정적 차이, §3.2) |
| 임베딩 백엔드 | local(minilm 384d) + openai(KURE 1024d) 병존 | **KURE(1024d) 단일 표준**, minilm 은 롤백용 Chroma 에만 잔존 |
| 코드 변경량 | — | 신규 `PgVectorStore`/`PgDocStore`(+graph 옵션), factory 분기, 공유 엔진 배선, 재임베딩 1회 |
| 롤백 난이도 | — | 낮음(기존 SQLite+Chroma 파일 보존 시 `STORE_BACKEND` 설정만 되돌림) |

---

## 3. 신규 `PgVectorStore` 설계

### 3.1 인터페이스 (ChromaStore와 동일하게)

`ChromaStore`(`chroma_store.py`) 의 공개 메서드를 **시그니처·반환·가드까지 1:1** 로 구현하여 호출부(`builder.py`/`query.py`)가 백엔드를 모르게 한다:

| 메서드 | ChromaStore 라인 | 시그니처 / 반환 | 미가용 가드 |
|--------|------------------|------------------|-------------|
| `add_texts` | 109-149 | `(texts, metadatas=None, ids=None) -> list[str]` | `RuntimeError` |
| `upsert_texts` | 151-170 | `(texts, metadatas=None, ids=None) -> list[str]` | `RuntimeError` |
| `query` | 176-221 | `(query_text, n_results=10, where=None) -> list[dict]` (키 `id`/`document`/`metadata`/`distance`) | `RuntimeError` |
| `get_by_id` | 223-235 | `(doc_id) -> dict \| None` | `RuntimeError` |
| `delete` | 237-241 | `(ids) -> None` | `RuntimeError` |
| `count` | 243-247 | `() -> int` | **0 반환(예외 아님)** |
| `reset_collection` | 249-263 | `() -> None` | `RuntimeError` |
| `available`/`ping` | 87-97 | 속성/`() -> bool` | — |

ID 생성 규칙도 동일하게 유지(`chroma_store.py:135-139,161-164`): `ids=None` 이면 `add_texts` 는 `sha256(f"{t}{time.time_ns()}")[:16]`, `upsert_texts` 는 `sha256(t)[:16]`(content-deterministic). `where` 필터는 metadata JSONB 조건(`metadata @> :filter` 또는 `metadata->>'key' = :val`)으로 번역한다.

### 3.2 결정적 차이 — 앱이 KURE EF 로 직접 임베딩 (KURE 단일 표준)

Chroma는 텍스트를 받아 내부에서 임베딩했다. pgvector 테이블에는 **앱이 벡터를 계산해 직접 INSERT** 해야 한다. **pgvector 의 표준 임베딩 백엔드는 KURE(`EMBEDDING_BACKEND=openai`, 1024d)로 확정**한다 — KURE 경로는 이미 명시적 EF 라 앱에서 그대로 호출 가능하고, 한국어 검색 품질(실측 MRR 1.000 vs minilm 0.285)도 우수하다.

- 기존 임베딩 함수를 **재사용**한다: `ResilientEmbeddingFunction`(`resilient_embedding.py`, primary=`OpenAIEmbeddingFunction`/fallback=`LlamaCppEmbeddingFunction`). Chroma `EmbeddingFunction` 시그니처(`__call__(input: list[str]) -> list[list[float]]`)를 따르므로 `PgVectorStore` 가 **동일 인터페이스로 호출**하여 벡터를 얻는다. `name()=="kure_v1"` 식별자도 유지.
- `add_texts`: `vectors = ef(texts)` → 각 `(id, vector, document, metadata)` 를 INSERT.
- `upsert_texts`: 같은 방식 + `ON CONFLICT (id) DO UPDATE`.
- `query`: `qvec = ef([query_text])[0]` → `ORDER BY embedding <=> :qvec LIMIT n_results` (cosine 연산자 `<=>`).
- **minilm(`EMBEDDING_BACKEND=local`, 384d)은 pgvector 표준에서 제외.** minilm 은 Chroma 내장 ONNX EF 에 의존해 앱 직접 호출 경로가 없으므로, 이를 PG 로 가져오려면 별도 EF 재구현이 필요하다 — 이 부담을 지지 않기 위해 minilm 은 **기존 Chroma 스택(롤백 경로, §10)에만 잔존**시킨다.

### 3.3 인덱스 / 거리

- 거리: cosine. pgvector 연산자 `<=>`(cosine distance). `ChromaStore` 가 `metadata={"hnsw:space": "cosine"}` 로 cosine을 쓰던 것과 일치.
- 인덱스: **HNSW** 우선(`USING hnsw (embedding vector_cosine_ops)`), 정확도/속도 우수하나 빌드 시 메모리·CPU 부담(RPi5 고려). 대안 **IVFFlat**(`USING ivfflat (embedding vector_cosine_ops) WITH (lists = ...)`)은 빌드가 가볍지만 `lists` 튜닝과 사전 데이터 적재가 필요. RPi5에서는 데이터 규모가 작으면 인덱스 없이 순차 스캔으로 시작 후 규모에 따라 HNSW 도입을 권장.

### 3.4 가용성/폴백 동작

`ChromaStore` 는 연결 실패 시 `available=False` 로 떨어지고 쓰기 시 `RuntimeError` 를 던진다(단 `count()` 는 0 반환). `PgVectorStore` 도 `_connect()` 실패 시 동일 패턴(`available=False`, `ping()`, `count→0`)을 유지해 호출부의 가드 로직을 보존한다.

### 3.5 SQLAlchemy 엔진 공유 / metadata 단순화

- **엔진 공유:** `PgVectorStore(engine=...)` 형태로 `make_sql_store` 가 만든 **동일 SQLAlchemy 엔진**(따라서 동일 커넥션 풀)을 주입받는다. `SQLStore` 가 이미 `create_engine` 위에서 동작하므로(§0), factory 가 엔진을 1회 생성해 sql/vector/doc 에 공유 주입하면 프로세스당 커넥션이 한 풀로 합쳐지고 스토어 간 교차 트랜잭션이 열린다.
- **metadata 정제 불필요:** Chroma 는 str/int/float/bool 만 허용해 `_sanitize_metadata`(`chroma_store.py:271-281`)로 평탄화했으나, JSONB 는 중첩 구조를 그대로 저장한다. 호환을 위해 당분간 동일 형태를 유지하되 `_sanitize_metadata` 단계는 제거 가능.
- **where 번역:** Chroma native `where` dict → JSONB(`metadata @> :filter` / `metadata->>'key' = :val`). 회귀 테스트에서 두 백엔드 동치 검증(§11).

---

## 4. 스키마 설계

### 4.1 vectors 테이블

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- KURE 단일 표준: 차원 1024 고정 (아래 4.3)
CREATE TABLE opencrab_vectors_kure (
    id        TEXT PRIMARY KEY,          -- ChromaStore와 동일한 16자 sha256 ID
    embedding vector(1024) NOT NULL,
    document  TEXT NOT NULL,
    metadata  JSONB NOT NULL DEFAULT '{}'
);

-- 규모 성장 후:
CREATE INDEX ON opencrab_vectors_kure USING hnsw (embedding vector_cosine_ops);
-- metadata 필터가 잦으면:
CREATE INDEX ON opencrab_vectors_kure USING gin (metadata);
```

`upsert_texts` 는 `INSERT ... ON CONFLICT (id) DO UPDATE SET embedding=EXCLUDED.embedding, document=EXCLUDED.document, metadata=EXCLUDED.metadata` 로 매핑.

`metadata` 는 Chroma가 string/int/float/bool 만 허용해 `_sanitize_metadata` 로 평탄화하던 제약이 사라진다. JSONB는 중첩 구조를 그대로 저장 가능하므로, 호환을 위해 당분간 동일 형태를 유지하되 향후 풍부한 메타데이터 저장이 가능하다.

### 4.2 doc / sql 테이블 매핑

- **sql**: `SQLStore._TABLES_SQL`(Postgres DDL)이 이미 존재 — `ontology_nodes`, `ontology_edges`, `impact_records`, `lever_simulations`, `rebac_policies`. 추가 작업 없이 URL만 Postgres로.
- **doc**: `LocalSQLDocStore` 의 3테이블(`doc_nodes`(PK `space,node_id`), `doc_sources`, `audit_log`)을 Postgres 동등 테이블로 옮긴다. `properties`/`metadata`/`details` 는 SQLite에서 JSON TEXT였으나 Postgres에서는 **JSONB**로 승격 권장(검색·필터·FTS 용이).

### 4.3 차원(N) 관리 — KURE 1024 고정

- pgvector 표준은 **KURE(`EMBED_DIM=1024`) 단일**이므로 PG 에는 `opencrab_vectors_kure vector(1024)` **한 테이블만 생성**한다. pgvector 의 `vector(N)` 차원 고정 제약을 KURE 단일화로 자연스럽게 해소.
- **minilm(384d) 테이블은 PG 에 만들지 않는다.** minilm 은 롤백용 Chroma 컬렉션(`opencrab_vectors`)에만 존재(§10). 향후 다른 차원 모델을 추가하면 그때 별도 테이블(`opencrab_vectors_<model>`)로 분리하는 전략을 따른다.

---

## 5. doc / sql 이전

- **sql**: `make_sql_store`(`factory.py:162-167`) 가 로컬에서 `settings.sqlite_url` 을 쓰는 분기를, 신규 플래그(`STORE_BACKEND=pgvector`)로 분기. `SQLStore(url=settings.postgres_url)` 만으로 동작(Postgres DDL 이미 보유).
- **doc**: `LocalSQLDocStore`(`local_sql_doc_store.py`) 와 동일 인터페이스를 갖는 `PgDocStore` 신규 작성(시그니처는 `MongoStore`/`LocalSQLDocStore` 호환 — `list_nodes(limit=...)`, `upsert_node_doc`, `get_node_doc`, sources, `log_event`/`get_audit_log`). `doc_nodes`(PK `space,node_id`)/`doc_sources`/`audit_log` 의 `properties`/`metadata`/`details` 는 JSON TEXT → **JSONB**. `list_nodes(limit=50000)` 은 BM25 재빌드 핫패스이므로(§7) Postgres에서도 인덱스된 `LIMIT` 스캔으로 O(k) 보장.
- **엔진 공유**: `PgDocStore`·`SQLStore`·`PgVectorStore` 모두 factory 가 1회 생성한 **동일 SQLAlchemy 엔진**을 주입받는다(§3.5). 단일 풀 + 교차 트랜잭션.
- 데이터 이전: SQLite → Postgres 로우 단위 복사(§8). JSON TEXT 컬럼은 `::jsonb` 캐스트.

---

## 6. graph 처리 — PG 그래프/Cypher 플러그인 평가

### 6.1 재현해야 할 표면 (LocalGraphStore)

`LocalGraphStore`(`local_graph_store.py:109-870`) 는 SQLite 인접 테이블 + 파이썬 BFS 이며 Neo4jStore 인터페이스를 미러한다. 단일-DB graph 백엔드는 다음 **16개 메서드와 그 의미를 동등 재현**해야 한다:

- 기본: `upsert_node`/`get_node`/`lookup_node_type`/`delete_node`/`upsert_edge`/`count_nodes`, 배치 `upsert_nodes_batch`/`upsert_edges_batch`(executemany + 단일 commit).
- 탐색: `find_neighbors`(BFS, depth/direction/limit), `find_path`(최단경로 BFS), `find_by_relations`(1-홉 relation 필터), `get_node_by_id`, `list_packs`(pack 집계), `export_nodes`/`export_edges`.
- Cypher: `run_cypher`(로컬은 **no-op** → 빈 결과; Neo4j 모드만 실제 Cypher).
- **반드시 보존할 미묘한 의미:**
  - **pack 필터 3규칙**(`_node_passes`/`_edge_passes`, `local_graph_store.py:42-75`): 외부 pack 노드/엣지 배제, `include_unpackaged` 분기.
  - **허브 노드 슬롯 로직**(`find_neighbors:384-393,423-431`): `remaining = limit - len(results)` 만큼 SQL `LIMIT` + 내부 `break`. 차수 615 허브의 32× 열화(d1 p50 0.37ms→11.86ms)를 막는 핵심.
  - **결정론**: Neo4j 대비 Jaccard ≈96.5%(엣지 삽입 순서 차이). reranker(RRF+BM25)가 최종 순위를 정하므로 영향 제한적.

### 6.2 Cypher 받는 PG 확장 조사 (사용자 요청)

| 옵션 | 형태 | Cypher | stock PG | aarch64/RPi5 | 상태 | 단일-DB 적합성 |
|------|------|--------|----------|--------------|------|----------------|
| **Apache AGE** | 확장 | ✅ openCypher(서브셋) | ✅ (PG 11–18) | 소스 빌드(배포 패키지 없음) | 활발(2026-01-21 릴리스: RLS·id 인덱스) | **Cypher 확장 중 유일 실질안** |
| Neo4j | **독립 JVM 서버** | ✅ 네이티브(가장 성숙) | ❌(별도 DB) | JVM 상시 RAM 부담, **Phase 1에서 기각** | 활발 | **부적합** — 별도 서버라 단일-DB 깨짐("떠나온 기준점") |
| AgensGraph | **PG 포크** | ✅ | ❌(PG 자체 대체, 16.9 기반) | 포크 빌드 | 활발(v2.16.0, 2026-06) | 부적합(stock PG 대체, 운영 이원화) |
| AgensGraph-Extension | 확장 | ✅ | ✅ | — | **2025-07 아카이브 → AGE 로 통합** | 폐기 |
| SQL/PGQ | PG 코어 네이티브 | ❌(SQL/PGQ 문법, ISO SQL:2023) | ✅(PG 19+) | — | **PG 19 미출시(2026-09 예정)** | 미래 전략(현재 불가) |
| pgRouting | 확장 | ❌(지리 라우팅 SQL) | ✅ | 패키지 | 활발 | 부적합(범용 그래프 아님) |
| 재귀 CTE | stock SQL | ❌ | ✅ | 불필요 | — | 폴백(확장 0, 결정론적) |

**Neo4j vs AGE 정리:** 이 통합의 목적이 "4스토어 → PG 한 DB"이므로 **Neo4j 는 구조적으로 탈락** — 쓰면 `PG + Neo4j` 두 서버가 되어 단일 백업·MVCC·교차 트랜잭션 동기(§1)가 무효화되고, localcrab 은 이미 Phase 1에서 RPi5 native 를 위해 Neo4j→SQLite 로 전환했다. **AGE 의 의의는 "Neo4j 의 Cypher 를 PG 안으로 흡수"** — `run_cypher` no-op 을 실 Cypher 경로로 부활시켜 기존 `Neo4jStore` Cypher 자산을 재사용하는 것이 최대 이점. 따라서 실질 선택지는 *Neo4j vs AGE* 가 아니라 **PG 내부의 AGE(Cypher) vs 재귀 CTE(무확장)** 다.

**조사 결론:** stock PG 위에서 **Cypher 를 받는 확장은 Apache AGE 가 사실상 유일**(AgensGraph-Extension 은 AGE 로 흡수·아카이브). AgensGraph 는 Cypher 를 받지만 PG 포크라 "단일 stock-PG" 목표와 상충. SQL/PGQ 는 PG19 출시 후의 **네이티브 미래 경로**(Cypher 아님)로 주시.

### 6.3 옵션과 롤아웃 권장

- **(A) Apache AGE** — Cypher 자산 재사용, 단일 DB·MVCC. 리스크: aarch64 소스 빌드, PG 버전 결속(11–18), §6.1 의 pack 3규칙·허브 LIMIT·결정론을 Cypher 로 1:1 재현 검증, 운영 학습비용.
- **(B) 재귀 CTE(stock PG)** — 확장 불필요(빌드 리스크 0). `WITH RECURSIVE` 로 `find_neighbors`/`find_path` 재작성, pack 필터는 JSONB(`properties->>'pack_id'`) + 사이클 가드, 허브는 `LIMIT`/lateral 로 슬롯 의미 근사. 단점: 16메서드 재구현·검증.
- **(C) SQLite graph 유지(단계적)** — 1차 위험 최소화(vectors/doc/sql 먼저 PG), graph 는 검증 후 (A)/(B).

**권장:** **(C)로 vectors/doc/sql 를 먼저 PG 통합** → graph 는 **AGE PoC 게이트**(aarch64 소스 빌드 성공 + Cypher 동등성 골든 테스트 통과, §11)를 넘으면 (A) 채택, 실패 시 (B) 재귀 CTE 폴백. PG19 GA 후 SQL/PGQ 재평가. 종착지는 graph 를 포함한 **완전 단일-DB**.

---

## 7. BM25 vs Postgres FTS (선택)

- 현재: 파이썬 인메모리 `BM25Index`(`opencrab/ontology/bm25.py`). `BM25Index.build(doc_store.list_nodes(limit=...))` 로 매 질의마다(스테일 핑거프린트 시) 재빌드. 한국어를 위해 `_tokenize` 가 Hangul 2/3-gram을 추가 생성하는 커스텀 토크나이저를 쓴다.
- 대안: Postgres `tsvector`/`tsquery` FTS. `doc_nodes` 에 `tsvector` 생성 컬럼 + GIN 인덱스를 두면 재빌드 없이 증분 검색이 가능하고 다중 프로세스가 공유한다.
- **주의:** Postgres 기본 FTS는 한국어 형태소 분석이 약하다(`pg_bigm`/`pgroonga` 같은 확장 필요, aarch64 빌드 부담). 현재 BM25의 Hangul n-gram·BM25 가중·pack/space 필터를 FTS로 1:1 재현하기 어렵다.
- **권장:** 이번 마이그레이션 범위에서 **제외**(BM25 파이썬 로직 유지). FTS는 별도 후속 과제로 분리. 단, `list_nodes` 가 Postgres로 옮겨가도 인터페이스가 동일하므로 BM25는 무변경으로 계속 동작한다.

---

## 8. 마이그레이션 절차

1. **PostgreSQL + pgvector 설치 (aarch64/RPi5).**
   - `apt install postgresql`, 그리고 pgvector: 배포판 패키지(`postgresql-16-pgvector` 등)가 있으면 apt, 없으면 소스 빌드(`make && make install`, `PG_CONFIG` 지정). aarch64 빌드 가능.
   - `CREATE EXTENSION vector;`
2. **DB/롤 생성.** `postgres_url` 기본값과 일치하는 롤/DB(`opencrab`/`opencrab`) 또는 `.env` 의 `POSTGRES_URL` 로 설정.
3. **공유 SQLAlchemy 엔진 부트스트랩.** factory 가 `create_engine(postgres_url)` 를 1회 생성해 sql/vector/doc 스토어에 주입(§3.5).
4. **스키마 생성.** sql 테이블은 `SQLStore` 가 자동 생성. doc 테이블(JSONB) + `opencrab_vectors_kure vector(1024)` 벡터 테이블 DDL 적용. 인덱스는 데이터 적재 후 생성(IVFFlat은 적재 후 필수, HNSW는 권장).
5. **전 벡터 재임베딩·재적재 (필수, KURE 단일).** Chroma 내부 벡터를 그대로 옮길 수 없다(임베딩 위치가 앱으로 이동). doc/노드 원본 텍스트에서 `ResilientEmbeddingFunction`(KURE) 으로 **재임베딩**하여 `opencrab_vectors_kure`(1024d)에 적재. minilm 재임베딩은 하지 않음(롤백용 Chroma 잔존).
6. **doc/sql 덤프 이전.** SQLite `doc_store.db`/`opencrab.db` 의 로우를 Postgres로 복사(JSON TEXT → JSONB 캐스트). 1회성 마이그레이션 스크립트(범위 외, 본 문서는 설계만).
7. **factory 분기.** `make_vector_store` 가 `PgVectorStore` 를, `make_doc_store`/`make_sql_store` 가 Postgres 백엔드를 선택하도록 신규 설정(`STORE_BACKEND=pgvector`) 추가. 기본값은 기존(local) 유지해 롤백 보장. graph 는 §6 롤아웃((C)→AGE/CTE)에 따름.
8. **검증.** §11 회귀 스위트(green→green) 전체 통과 — 행 수 대조(`count()` vs Chroma, `table_counts()`), 대표 질의 top-k(특히 KURE 한국어 MRR), BM25 결과 동등성, graph 백엔드 parity, 다중 프로세스 동시 적재 스모크.

---

## 9. 동시성 결론

PostgreSQL MVCC로 **MCP 서버 + 백그라운드 로더(또는 다중 클라이언트)가 락 없이 동시 가동**된다. 리더는 라이터를 막지 않고, 라이터는 행 단위로만 경합한다. 이는 `[[ingestion-via-mcp-plan]]` 이 요구하던 "단일 라이터 직렬화" 우회를 불필요하게 만든다(Chroma 단일프로세스 제약과 SQLite 라이터 직렬화가 모두 해소). 즉 본 통합은 동시성 문제의 **근본 해소**다.

---

## 10. 리스크 / 롤백

**리스크**

- **재임베딩 비용.** 전 벡터를 1회 KURE로 재생성해야 함(임베딩 서버/GGUF 부하). 데이터 규모에 비례한 시간·CPU 소요.
- **RPi5 Postgres 부담.** 상시 서버 프로세스 + `shared_buffers`/`work_mem`/`maintenance_work_mem` 메모리, HNSW 인덱스 빌드 시 CPU·메모리 스파이크. 인프로세스 SQLite/Chroma 대비 상시 자원 점유 증가. → `shared_buffers` 보수적 설정, 인덱스는 규모 따라 단계적 도입으로 완화.
- **pgvector aarch64 빌드.** 배포판 패키지 부재 시 소스 빌드 필요.
- **Apache AGE aarch64 빌드/버전 결속(graph 옵션 A 채택 시).** 배포 패키지 없음 → 소스 빌드 + PG 메이저 버전(11–18) 헤더 일치 필요. PoC 게이트(§11)를 통과 못 하면 재귀 CTE(B)로 폴백.
- **graph 옵션(B) 채택 시** 재귀 CTE 재구현·성능 검증 리스크(허브 LIMIT·pack 3규칙·결정론 재현).

> minilm 직접임베딩 리스크는 **KURE 단일 표준화로 해소**(§3.2) — pgvector 에서 minilm 을 재구현하지 않는다.

**롤백 경로**

- **기존 SQLite 파일과 Chroma 디렉터리를 보존**한다(삭제 금지). factory 분기 기본값을 기존(local)으로 두면, `STORE_BACKEND`/`EMBEDDING_BACKEND` 등 **설정만 되돌려** 즉시 기존 스택으로 복귀 가능. minilm(384d)은 롤백용 Chroma 컬렉션(`opencrab_vectors`)에 그대로 남아 있고, PG 의 `opencrab_vectors_kure`(1024d)는 별도 테이블이라 원복이 비파괴적이다.

---

## 11. 테스트 전략 — 회귀 검증(green→green, 특성화/parity)

이건 새 동작을 만드는 TDD(red→green)가 **아니다**. 기존 동작 보존을 검증하는 마이그레이션이므로 **green→green**: 테스트를 **현행 SQLite/Chroma 구현에 대해 먼저 통과(green)** 시켜 기존 동작을 골든으로 고정 → PG 백엔드 구현 → **동일 테스트가 백엔드만 바뀐 채 그대로 통과(green)**. 어느 단계에서도 red 가 나면 안 된다(red = 회귀).

- **필수 순서:** ① 현행 백엔드로 특성화 테스트 작성·실행 → **green(베이스라인 고정)** → ② PG 백엔드 구현 → ③ **백엔드 파라미터라이즈**(`local`/`pg`)로 같은 스위트 재실행 → **green(parity 확인)**.
- **정상(happy):** `add_texts`/`upsert_texts` 후 `query` top-k, `count()` 정합, `get_by_id`, doc `upsert_node_doc`/`list_nodes(limit)` O(k), graph `find_neighbors`/`find_path` 기대 이웃 — **두 백엔드 동일 결과**.
- **실패(failure):** 미가용 시 `available=False`+쓰기 `RuntimeError`(`count()`→0) — ChromaStore 가드를 **PG 도 동일 재현**. `vector(1024)` 차원 불일치 INSERT 거부, EF 예외 시 `ResilientEmbeddingFunction` fallback 경로.
- **엣지(edge):** 빈 입력(`[]`→`[]`), 중복 ID upsert 멱등, `where` 필터(중첩/없음) — Chroma where ↔ JSONB 번역 동치, 한국어 텍스트, 허브 노드 차수>limit 슬롯 동작, pack 필터 3규칙(외부 pack 노드/엣지 배제, `include_unpackaged` 분기), 사이클 그래프 — **현행 동작과 동치**.
- **회귀 기준선(green 유지 대상):** KURE 한국어 top-k **MRR(현행 1.000)**, Chroma↔pgvector `count()`·`table_counts()` parity, BM25 결과 동등성, graph parity(LocalGraphStore 골든 vs AGE/CTE — `find_neighbors`/`find_path`/`list_packs`, Jaccard 임계).
- **픽스처:** `tests/` 의 `local_stores` 패턴(monkeypatch `LOCAL_DATA_DIR`)을 **백엔드 파라미터라이즈** `stores(backend)`(`local` 임시 dir / `pg` 임시 스키마)로 확장. 임베딩은 **결정적 mock EF**(`__call__` 고정 벡터)로 외부 LM Studio/GGUF 의존 제거(두 백엔드 동일 벡터 → 결과 동치 비교). PG 테스트는 `@pytest.mark.skipif(no PG)`.
- **동시성 스모크:** MCP 서버 + 백그라운드 로더 동시 적재(MVCC 락 무경합) — 현행 `test_store_concurrency.py` 패턴 PG 버전.
- 실행: `pytest`(`pyproject.toml:92` testpaths=tests, asyncio auto). 회귀 게이트 = **전체 스위트가 두 백엔드 모두 green**.

---

## 12. 병렬 작업 방식 — 다이나믹 워크플로우

구현은 **독립 워크스트림 4갈래**를 **병렬 다이나믹 워크플로우**로 실행한다(각자 git worktree 격리로 파일 충돌 방지):

1. `PgVectorStore` (+ KURE EF 재사용, `opencrab_vectors_kure` DDL)
2. `PgDocStore` (`LocalSQLDocStore` 인터페이스, JSONB)
3. graph 백엔드 PoC (AGE aarch64 소스 빌드 + Cypher 골든 / 폴백 재귀 CTE)
4. `factory` 분기 + 1회성 마이그레이션 스크립트 + 공유 엔진 배선(`builder.py` 4중 fan-out)

- **각 워크스트림 내부도 green→green 파이프라인**(현행 특성화 테스트 통과 → PG 구현 → 동일 테스트 parity 통과)으로 단계화. 스트림 간 의존 없는 stage 는 pipeline 으로 무배리어 진행.
- **합류 지점:** 공유 SQLAlchemy 엔진 시그니처와 `Settings` 신규 플래그(`STORE_BACKEND`)를 **먼저 합의(인터페이스 동결)** 후 분기. 최종 회귀(§11 전체 스위트)는 합류 후 일괄 게이트.
- **착수 전 세션 컴팩트** 권장(탐색·조사로 컨텍스트 누적).
