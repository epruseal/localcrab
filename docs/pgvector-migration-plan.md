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
- **`SQLStore` 는 이미 PostgreSQL을 지원한다.** `__init__(url)` 에서 `url.startswith("sqlite")` 로 분기하며 SERIAL/TIMESTAMPTZ DDL(`_TABLES_SQL`)과 SQLite DDL(`_TABLES_SQL_SQLITE`)을 모두 갖고 있다. 설정값은 `config.py` 의 `postgres_url`(alias `POSTGRES_URL`, 기본 `postgresql://opencrab:opencrab@localhost:5432/opencrab`). 현재 `make_sql_store` 는 `settings.sqlite_url if settings.is_local else settings.postgres_url` 로 로컬에서 SQLite를 강제한다.
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

---

## 2. 아키텍처 비교표

| 항목 | 현행 (SQLite + Chroma) | 제안 (PostgreSQL + pgvector) |
|------|------------------------|------------------------------|
| 시스템 수 | SQLite 파일 3개(graph/doc/sql) + Chroma 디렉터리 | Postgres 1개 서버 (graph는 옵션 6에 따라 1개 더 또는 통합) |
| 벡터 동시 쓰기 | 단일 프로세스(Chroma 제약) | 다중 프로세스(MVCC) |
| doc/sql 동시 쓰기 | WAL 다중 프로세스(라이터 직렬화) | MVCC 행 단위 |
| 리더-라이터 | SQLite WAL: 리더 비차단 | MVCC: 리더 비차단 |
| 백업 | 파일 복사(시점 불일치 위험) | `pg_dump` 단일 정합 스냅샷 |
| RPi5 부담 | 매우 낮음(인프로세스) | 상시 서버 프로세스 + shared_buffers, HNSW 빌드 시 CPU/메모리 ↑ |
| 임베딩 위치 | Chroma 자동 | **앱이 직접 계산 후 INSERT** (결정적 차이) |
| 코드 변경량 | — | 신규 `PgVectorStore`, doc용 Postgres 스토어, factory 분기, 재임베딩 1회 |
| 롤백 난이도 | — | 낮음(기존 SQLite+Chroma 파일 보존 시 설정만 되돌림) |

---

## 3. 신규 `PgVectorStore` 설계

### 3.1 인터페이스 (ChromaStore와 동일하게)

`ChromaStore` 의 공개 메서드를 그대로 구현하여 호출부가 백엔드를 모르게 한다:

- `add_texts(texts, metadatas=None, ids=None) -> list[str]`
- `upsert_texts(texts, metadatas=None, ids=None) -> list[str]`
- `query(query_text, n_results=10, where=None) -> list[dict]` — 반환 키: `id`, `document`, `metadata`, `distance`
- `get_by_id(doc_id) -> dict | None`
- `delete(ids) -> None`
- `count() -> int`
- `reset_collection() -> None`
- 보조: `available` 속성, `ping()`

ID 생성 규칙도 `ChromaStore` 와 동일하게 유지: `ids=None` 이면 `add_texts` 는 `sha256(f"{t}{time.time_ns()}")[:16]`, `upsert_texts` 는 `sha256(t)[:16]`. `where` 필터는 metadata JSONB 조건(`metadata @> :filter` 또는 `metadata->>'key' = :val`)으로 번역한다.

### 3.2 결정적 차이 — 앱이 직접 임베딩

Chroma는 텍스트를 받아 내부에서 임베딩했다. pgvector 테이블에는 **앱이 벡터를 계산해 직접 INSERT** 해야 한다.

- 기존 임베딩 함수를 **재사용**한다: `ResilientEmbeddingFunction`(`opencrab/stores/resilient_embedding.py`), 그 안의 `OpenAIEmbeddingFunction`/`LlamaCppEmbeddingFunction`. 이들은 Chroma `EmbeddingFunction` 시그니처(`__call__(input: list[str]) -> list[list[float]]`)를 따르므로 `PgVectorStore` 가 동일 인터페이스로 호출하여 벡터를 얻는다.
- `add_texts`: `vectors = ef(texts)` → 각 `(id, vector, document, metadata)` 를 INSERT.
- `query`: `qvec = ef([query_text])[0]` → `ORDER BY embedding <=> :qvec LIMIT n_results` (cosine 연산자 `<=>`).
- `EMBEDDING_BACKEND=local`(minilm 384d, Chroma 기본 EF)을 pgvector에서도 쓰려면, 현재 Chroma에 의존하던 기본 EF를 앱에서 직접 호출 가능한 형태로 확보해야 한다(예: minilm용 임베딩 함수를 명시 주입). minilm 자동임베딩을 더는 Chroma에 위임할 수 없다는 점이 설계상 유일한 추가 부담이다. KURE(openai) 경로는 이미 명시적 EF라 그대로 재사용된다.

### 3.3 인덱스 / 거리

- 거리: cosine. pgvector 연산자 `<=>`(cosine distance). `ChromaStore` 가 `metadata={"hnsw:space": "cosine"}` 로 cosine을 쓰던 것과 일치.
- 인덱스: **HNSW** 우선(`USING hnsw (embedding vector_cosine_ops)`), 정확도/속도 우수하나 빌드 시 메모리·CPU 부담(RPi5 고려). 대안 **IVFFlat**(`USING ivfflat (embedding vector_cosine_ops) WITH (lists = ...)`)은 빌드가 가볍지만 `lists` 튜닝과 사전 데이터 적재가 필요. RPi5에서는 데이터 규모가 작으면 인덱스 없이 순차 스캔으로 시작 후 규모에 따라 HNSW 도입을 권장.

### 3.4 가용성/폴백 동작

`ChromaStore` 는 연결 실패 시 `available=False` 로 떨어지고 쓰기 시 `RuntimeError` 를 던진다. `PgVectorStore` 도 `_connect()` 실패 시 동일 패턴(`available=False`, `ping()`)을 유지해 호출부의 가드 로직을 보존한다.

---

## 4. 스키마 설계

### 4.1 vectors 테이블

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE opencrab_vectors (
    id        TEXT PRIMARY KEY,          -- ChromaStore와 동일한 16자 sha256 ID
    embedding vector(N) NOT NULL,        -- N = 차원 (아래 4.3)
    document  TEXT NOT NULL,
    metadata  JSONB NOT NULL DEFAULT '{}'
);

-- 규모 성장 후:
CREATE INDEX ON opencrab_vectors USING hnsw (embedding vector_cosine_ops);
-- metadata 필터가 잦으면:
CREATE INDEX ON opencrab_vectors USING gin (metadata);
```

`upsert_texts` 는 `INSERT ... ON CONFLICT (id) DO UPDATE SET embedding=EXCLUDED.embedding, document=EXCLUDED.document, metadata=EXCLUDED.metadata` 로 매핑.

`metadata` 는 Chroma가 string/int/float/bool 만 허용해 `_sanitize_metadata` 로 평탄화하던 제약이 사라진다. JSONB는 중첩 구조를 그대로 저장 가능하므로, 호환을 위해 당분간 동일 형태를 유지하되 향후 풍부한 메타데이터 저장이 가능하다.

### 4.2 doc / sql 테이블 매핑

- **sql**: `SQLStore._TABLES_SQL`(Postgres DDL)이 이미 존재 — `ontology_nodes`, `ontology_edges`, `impact_records`, `lever_simulations`, `rebac_policies`. 추가 작업 없이 URL만 Postgres로.
- **doc**: `LocalSQLDocStore` 의 3테이블(`doc_nodes`(PK `space,node_id`), `doc_sources`, `audit_log`)을 Postgres 동등 테이블로 옮긴다. `properties`/`metadata`/`details` 는 SQLite에서 JSON TEXT였으나 Postgres에서는 **JSONB**로 승격 권장(검색·필터·FTS 용이).

### 4.3 차원(N) 관리

- minilm(local): **384**. KURE(openai): **1024** (`EMBED_DIM`).
- pgvector 컬럼은 `vector(N)` 으로 차원이 고정된다. 백엔드 전환 시 차원이 바뀌므로 **별도 테이블/스키마로 분리**하는 것이 안전하다(현재 Chroma가 `opencrab_vectors` vs `opencrab_vectors_kure` 컬렉션으로 분리한 것과 동일한 전략).
  - 예: `opencrab_vectors`(384) / `opencrab_vectors_kure`(1024). factory가 `embedding_backend`/`embed_dim` 으로 테이블명·차원을 선택.

---

## 5. doc / sql 이전

- **sql**: `make_sql_store` 가 로컬에서 `settings.sqlite_url` 을 쓰는 분기를, "Postgres URL이 설정되면 우선" 으로 바꾸거나 신규 플래그(예: `STORE_BACKEND=pgvector`)로 분기. `SQLStore(url=settings.postgres_url)` 만으로 동작(Postgres DDL 이미 보유).
- **doc**: `LocalSQLDocStore` 와 동일 인터페이스를 갖는 Postgres 백엔드 신규 작성(메서드 시그니처는 `MongoStore`/`LocalSQLDocStore` 호환 유지 — `list_nodes(limit=...)`, upsert, sources, audit). `list_nodes(limit=50000)` 은 BM25 재빌드 핫패스이므로(아래 7) Postgres에서도 인덱스된 `LIMIT` 스캔으로 O(k) 보장.
- 데이터 이전: SQLite → Postgres 로우 단위 복사(아래 8). JSON TEXT 컬럼은 `::jsonb` 캐스트.

---

## 6. graph 처리 옵션

`LocalGraphStore` 는 SQLite 인접 테이블 + 파이썬 BFS(`find_neighbors`/`find_path`)다.

- **(a) SQLite 유지.** 그래프만 SQLite로 남기고 vectors/doc/sql 만 Postgres로. 장점: 변경 최소, BFS 코드 그대로. 단점: 통합 미완성(파일 1개 + 서버 1개 공존), 백업 일원화 불완전, graph↔doc/sql 교차 트랜잭션 불가.
- **(b) Postgres 테이블 + 재귀 CTE.** `graph_edges(from_id, relation, to_id, ...)` 테이블에 `WITH RECURSIVE` 로 `find_neighbors`(depth 제한 순회)/`find_path`(경로 탐색)를 SQL로 재구현. 장점: 완전 통합, MVCC 동시성, 단일 백업, 교차 조인 가능. 단점: 파이썬 BFS → 재귀 CTE 재작성·검증 비용, 깊은 그래프에서 CTE 성능 튜닝 필요(방문 집합·사이클 가드).

**권장:** 1차로 (a)로 위험을 줄여 vectors/doc/sql 통합을 먼저 달성하고, 그래프는 검증 후 (b)로 점진 이전.

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
3. **스키마 생성.** sql 테이블은 `SQLStore` 가 자동 생성. doc 테이블 + `opencrab_vectors[_kure]` 벡터 테이블 DDL 적용. 인덱스는 데이터 적재 후 생성(IVFFlat은 적재 후 필수, HNSW는 권장).
4. **전 벡터 재임베딩·재적재 (필수).** Chroma 내부 벡터를 그대로 옮길 수 없다(임베딩 위치가 앱으로 이동). doc/노드 원본 텍스트에서 `ResilientEmbeddingFunction` 으로 **재임베딩**하여 `opencrab_vectors[_kure]` 에 적재. 차원은 백엔드에 맞춤(384/1024).
5. **doc/sql 덤프 이전.** SQLite `doc_store.db`/`opencrab.db` 의 로우를 Postgres로 복사(JSON TEXT → JSONB 캐스트). 1회성 마이그레이션 스크립트(범위 외, 본 문서는 설계만).
6. **factory 분기.** `make_vector_store` 가 `PgVectorStore` 를, `make_doc_store`/`make_sql_store` 가 Postgres 백엔드를 선택하도록 신규 설정(예: `STORE_BACKEND=pgvector`) 추가. 기본값은 기존(local) 유지해 롤백 보장.
7. **검증.** 행 수 대조(`count()` vs Chroma `count()`, `table_counts()`), 대표 질의의 top-k 회귀(특히 KURE 한국어 MRR), BM25 결과 동등성, 다중 프로세스 동시 적재 스모크 테스트.

---

## 9. 동시성 결론

PostgreSQL MVCC로 **MCP 서버 + 백그라운드 로더(또는 다중 클라이언트)가 락 없이 동시 가동**된다. 리더는 라이터를 막지 않고, 라이터는 행 단위로만 경합한다. 이는 `[[ingestion-via-mcp-plan]]` 이 요구하던 "단일 라이터 직렬화" 우회를 불필요하게 만든다(Chroma 단일프로세스 제약과 SQLite 라이터 직렬화가 모두 해소). 즉 본 통합은 동시성 문제의 **근본 해소**다.

---

## 10. 리스크 / 롤백

**리스크**

- **재임베딩 비용.** 전 벡터를 1회 재생성해야 함(특히 KURE는 임베딩 서버/GGUF 부하). 데이터 규모에 비례한 시간·CPU 소요.
- **RPi5 Postgres 부담.** 상시 서버 프로세스 + `shared_buffers`/`work_mem`/`maintenance_work_mem` 메모리, HNSW 인덱스 빌드 시 CPU·메모리 스파이크. 인프로세스 SQLite/Chroma 대비 상시 자원 점유 증가. → `shared_buffers` 보수적 설정, 인덱스는 규모 따라 단계적 도입으로 완화.
- **minilm 직접 임베딩 확보.** local 백엔드에서 Chroma 자동임베딩에 의존하던 minilm 384d를 앱에서 직접 호출할 경로 필요(3.2).
- **pgvector aarch64 빌드.** 배포판 패키지 부재 시 소스 빌드 필요.
- **graph 옵션(b) 채택 시** 재귀 CTE 재구현·성능 검증 리스크.

**롤백 경로**

- **기존 SQLite 파일과 Chroma 디렉터리를 보존**한다(삭제 금지). factory 분기 기본값을 기존(local)으로 두면, `STORE_BACKEND`/`EMBEDDING_BACKEND` 등 **설정만 되돌려** 즉시 기존 스택으로 복귀 가능. Chroma의 컬렉션 분리 전략(`opencrab_vectors` vs `opencrab_vectors_kure`)이 이미 차원 비호환을 막아두었으므로, pgvector 테이블도 별도라 원복이 비파괴적이다.
```
