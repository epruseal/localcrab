# 스토어 단일화 마이그레이션 플랜 (SQLite-unified vs PG-unified)

> 상태: 설계 문서(Design only). 코드 구현 없음.
> 선행 플랜 상호 참조: `[[ingestion-via-mcp-plan]]` (동시성의 1단계 — MCP 단일 라이터 경유 적재).
> 본 문서는 그 다음 단계인 **스토어 통합** 옵션이다.
> 두 단일규율 타깃을 병렬 비교한다: **(A) SQLite-unified(Chroma→sqlite-vec)** / **(B) PG-unified(pgvector)**.
> **§9 의사결정 힌지(다중 라이터 요구 여부)**로 둘 중 하나를 선택한다.

> 본 문서는 원래 pgvector(B) 전용이었으나, 문서 자신이 §0/§2에서 밝히는 사실 — *동시성의 약한 고리는
> Chroma 하나뿐이고 doc/sql/graph 는 이미 SQLite WAL* — 에서 자연히 도출되는 **(A) SQLite-unified(sqlite-vec)**
> 경로를 대등하게 병기하도록 확장되었다. 파일명(`pgvector-migration-plan.md`)은 이력상 유지한다.

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
- **⇒ 단일규율 달성 경로가 둘이다(본 문서의 핵심 프레임).** 약한 고리는 Chroma **하나뿐**이고 graph/doc/sql 3스토어는 **이미 SQLite WAL**이다. 따라서 "한 규율로 통일"하는 길은:
  - **(A) 아래로 통합 = SQLite 단일**: Chroma → **sqlite-vec**(vec0 가상테이블) 교체 → 4스토어 전부 SQLite WAL. `LocalSQLDocStore` 가 이미 확립한 스레드-로컬 커넥션 + write 락 + WAL 규율을 그대로 재사용. **신규 인프라 0**, graph/doc/sql/FTS5 무변경.
  - **(B) 위로 통합 = Postgres 단일**: 전 스토어를 PG/pgvector 로. **MVCC 다중 라이터**. Postgres 데몬 + graph 재구현(AGE/CTE).
  - 본 문서는 이 둘을 §1~§13 전반에서 **병렬**로 다루고, **§9 힌지**로 최종 선택한다. sqlite-vec 는 **임베딩 백엔드가 아니라 벡터 스토어 백엔드**(설정 축 = `STORE_BACKEND`, 임베딩은 KURE 유지)임에 유의.

---

## 1. 동기 (Motivation)

> 아래 동기 각각을 **어느 옵션이 제공하는가**로 태깅한다. (A)=SQLite-unified(sqlite-vec), (B)=PG-unified(pgvector).

1. **단일 서버/규율 통합.** `[A·B 공통]` 흩어진 벡터 백엔드를 나머지 스토어와 한 규율로 모은다. (B)는 vectors+doc+sql 을 **PostgreSQL 한 서버**에, (A)는 vectors 를 나머지 3스토어와 **같은 SQLite WAL 규율**에 편입한다. 어느 쪽이든 운영 대상(현재 SQLite 3파일 + Chroma 디렉터리)이 단순화된다. 둘 다 Docker 불필요.
2. **MVCC 동시성.** `[B 전용]` PostgreSQL은 행 단위 락 + 스냅샷 격리(MVCC)로 **리더는 라이터를 막지 않고**, 라이터는 행 단위로만 경합한다. 다중 프로세스(MCP 서버 + 백그라운드 로더)가 락 충돌 없이 동시에 읽고 쓸 수 있다. **(A) sqlite-vec 는 이 다중 라이터를 제공하지 않는다**(SQLite 단일 라이터 직렬화 유지 — §9).
3. **표준 백업.** `[B 전용(강)]` `pg_dump`/`pg_restore`/PITR 로 vectors·doc·sql 을 한 번에 정합성 있게 백업·복구. `[A 부분]` (A)도 전부 SQLite 파일이 되어 **파일 복사 백업이 단일 규율로 일관**되나, Chroma 디렉터리처럼 벡터/메타 시점 불일치 위험은 SQLite 다파일 간에 여전히 남는다(단일 파일 편입 시 완화).
4. **Chroma 단일프로세스 제약 해소.** `[A·B 공통]` 동시성의 약한 고리였던 Chroma 를 제거한다. (B)는 MVCC로, (A)는 **SQLite WAL(다중 프로세스 읽기 + 라이터 직렬화)**로 대체한다. `[[ingestion-via-mcp-plan]]` 의 "단일 라이터 직렬화" **우회 자체가 불필요**해지는 것은 **(B)뿐**이고, (A)는 그 직렬화를 SQLite 규율로 **정식 편입**(현행 stop-to-load 현실과 정합).
5. **단일 SQLAlchemy 엔진/풀 공유.** `[B 전용]` `SQLStore` 가 이미 SQLAlchemy 엔진을 쓰므로(§0), vector/doc/(graph) 스토어가 같은 엔진을 주입받으면 4개 스토어가 **하나의 커넥션 풀**로 동작한다. 프로세스당 커넥션 수가 줄고, 스토어 간 **교차 트랜잭션·조인**(예: 노드 등록과 벡터 INSERT 를 한 트랜잭션으로)이 가능해진다. (A)는 이 교차 트랜잭션을 얻지 못하나(스토어별 SQLite 파일/커넥션), 같은 db 파일 편입 시 부분적으로 근사 가능.

**(A) SQLite-unified 전용 이점(위 목록에 없는 것):**
- **신규 인프라 0.** Postgres 데몬·롤·shared_buffers 없음. RPi5 상시 자원 점유 증가 없음(인프로세스 유지).
- **graph/FTS5/doc/sql 무변경.** sqlite-vec 는 벡터만 교체 → §6 의 AGE vs 재귀CTE 재구현(플랜 최대 난공사)·§7 tsvector 전환이 **통째로 불필요**.
- **기존 SQLite 규율 재사용.** `LocalSQLDocStore` 의 스레드-로컬+write락+WAL(이미 하드닝·회귀테스트 존재)을 그대로 차용 → 신규 동시성 코드 최소.

---

## 2. 아키텍처 비교표

| 항목 | 현행 (SQLite + Chroma) | **(A) SQLite-unified (sqlite-vec)** | **(B) PG-unified (pgvector)** |
|------|------------------------|-------------------------------------|-------------------------------|
| 시스템 수 | SQLite 3파일(graph/doc/sql) + Chroma 디렉터리 | **SQLite 만**(graph/doc/sql + vec0). 신규 데몬 0 | **Postgres 1서버 + 단일 SQLAlchemy 엔진/풀**(graph 통합 시 4스토어 한 DB) |
| 벡터 동시 쓰기 | 단일 프로세스(Chroma 제약) | **단일 라이터 직렬화(SQLite WAL)** | **다중 프로세스(MVCC)** |
| doc/sql 동시 쓰기 | WAL 다중 프로세스(라이터 직렬화) | 동일(WAL, 무변경) | MVCC 행 단위 |
| 리더-라이터 | SQLite WAL: 리더 비차단 | SQLite WAL: 리더 비차단 | MVCC: 리더 비차단 |
| graph 백엔드 | `LocalGraphStore`(SQLite+파이썬 BFS) | **무변경**(sqlite-vec 는 벡터만 교체) | AGE 또는 재귀 CTE 로 **재구현**(§6) |
| 키워드 FTS | SQLite FTS5(`doc_sources_fts`, §7.1) | **무변경**(FTS5 유지) | tsvector/`pg_bigm` 전환(후속, §7) |
| metadata 필터 | Chroma `where` dict | **vec0**: metadata 컬럼 16개(`= != < <= > >=`) + **partition key(pack_id 사전필터)** | **JSONB + GIN**(임의 중첩 쿼리) |
| 백업 | 파일 복사(시점 불일치 위험) | 파일 복사(전부 SQLite, 규율 일관) | `pg_dump` 단일 정합 스냅샷 |
| RPi5 부담 | 매우 낮음(인프로세스) | **매우 낮음(인프로세스 유지)** | 상시 서버 + shared_buffers, HNSW 빌드 시 CPU/메모리 ↑ |
| ANN 인덱스 | Chroma HNSW | **브루트포스**(ANN=IVF/DiskANN stable 미확정, §3-SQLite) | HNSW/IVFFlat(성숙) |
| 성숙도 | 안정 | **pre-v1(v0.1.9, 파괴적 변경 예고)** | 프로덕션급 |
| 임베딩 위치 | Chroma 자동(minilm) / KURE explicit EF | **앱이 KURE EF 로 직접 계산 후 INSERT**(§3.2 공유) | **앱이 KURE EF 로 직접 계산 후 INSERT**(§3.2 공유) |
| 임베딩 백엔드 | local(minilm 384d)+openai(KURE 1024d) 병존 | **KURE(1024d) 단일**, minilm 은 롤백용 Chroma 잔존 | **KURE(1024d) 단일**, minilm 은 롤백용 Chroma 잔존 |
| 재임베딩 | — | **1회 필수**(Chroma 내부벡터 이전 불가) | **1회 필수**(동일) |
| 코드 변경량 | — | 신규 `SqliteVecStore` 1개 + factory 분기 + 재임베딩. **doc/sql/graph/FTS 무변경** | 신규 `PgVectorStore`/`PgDocStore`(+graph 옵션), factory 분기, 공유 엔진 배선, 재임베딩 |
| 롤백 난이도 | — | 낮음(Chroma 보존 + `STORE_BACKEND` 되돌림) | 낮음(Chroma 보존 + `STORE_BACKEND` 되돌림) |

### 2.1 의사결정 매트릭스

두 옵션은 경쟁이 아니라 **성장 단계**로 볼 수도 있으나, 선택을 가르는 단일 힌지는 **"진짜 다중 라이터가 확정 요구인가"**다(상세는 §9):

| 조건 | 선택 |
|------|------|
| 로더가 MCP **정지 후** 적재(현행 stop-to-load), 읽기 지배, 실시간 동시 write 미요구 | **(A) sqlite-vec** — 통증(Chroma) 대비 압도적으로 싸다. Postgres·AGE·graph 재구현 0 |
| 로더가 MCP **서빙 중** 동시 write 필요(`[[ingestion-via-mcp-plan]]` 실시간 적재 확정), 다중 serve 동시 write, 벡터 수백만 스케일 | **(B) pgvector** — MVCC 가 필수. (A)는 구조적 막다른 길 |
| 미확정 | Phase 2 에서 **둘을 같은 §11.1 게이트로 나란히 벤치** 후 결정 |

> **요지:** (A)는 "아래로 통합(SQLite 규율에 벡터를 흡수)", (B)는 "위로 통합(전부 PG, MVCC)". 둘 다 Chroma 약한 고리를 제거하지만, (B)의 추가 무게(RPi Postgres + AGE + graph 재구현)를 지불할 유일한 이유가 **MVCC 다중 라이터**다. 그게 현재 미사용 자산이면 (A)로 충분하다.

---

## 3. 신규 벡터 스토어 설계 (PgVectorStore / SqliteVecStore)

### 3.0 두 옵션의 공유 트레잇

(A) `SqliteVecStore` 와 (B) `PgVectorStore` 는 **다음 두 가지를 동일하게** 따른다. 아래 §3.1~§3.5 는 (B) 전용 상세이나, **§3.1 인터페이스와 §3.2 앱측 임베딩·§3.4 폴백 가드는 (A)에도 1:1 그대로 적용**된다(§3.6 참조).

- **인터페이스 1:1(§3.1).** 둘 다 `ChromaStore` 의 공개 메서드를 시그니처·반환·가드까지 동일 재현 → 호출부(`builder.py:147-166`, `query.py:620-667`)가 백엔드를 모른다.
- **앱측 임베딩(§3.2).** Chroma 는 텍스트를 받아 내부 임베딩했지만, sqlite-vec/pgvector 는 **원시 벡터를 저장**하므로 앱이 `ResilientEmbeddingFunction`(KURE, `__call__(list[str])->list[list[float]]`)으로 **직접 계산 후 INSERT** 한다. 임베딩 경로는 **스토어와 무관하게 동일**하고, 바뀌는 것은 저장/검색 백엔드뿐이다. 설정 축은 `EMBEDDING_BACKEND`(임베딩)가 아니라 **`STORE_BACKEND`(벡터 스토어)** 다.

> **§3.1~§3.5 는 (B) PgVectorStore 상세.** (A) SqliteVecStore 상세는 **§3.6**.

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

### 3.6 신규 `SqliteVecStore` 설계 (A 옵션)

sqlite-vec(`asg017/sqlite-vec`)의 **vec0 가상테이블**을 백엔드로 쓰는 벡터 스토어. **§3.1 인터페이스·§3.2 앱측 임베딩·§3.4 폴백 가드를 그대로 재사용**하고, 아래만 pgvector 와 다르다.

- **저장 = vec0 가상테이블**(§4 DDL). `pack_id` 를 **partition key** 로 선언해 pack isolation 을 **사전필터(pre-filter)**로 처리하고, `document`/`metadata(JSON)` 는 **auxiliary 컬럼(`+`접두)**(SELECT 반환 전용)에 둔다. ID 규칙(sha256 16자)은 §3.1 과 동일.
- **`add_texts`/`upsert_texts`:** `vectors = ef(texts)` → `(node_id, pack_id, embedding, document, metadata)` INSERT. upsert 는 `node_id` 기준 삭제-후-삽입 또는 `INSERT OR REPLACE`(vec0 upsert 시맨틱은 구현 착수 시 확인).
- **`query`:** `qvec = ef([query_text])[0]` → vec0 KNN(`WHERE embedding MATCH :qvec AND k = :n [AND pack_id = :p] ORDER BY distance`). 반환 dict 키(`id`/`document`/`metadata`/`distance`)는 §3.1 과 동일. **`where` 번역:** Chroma dict → vec0 `WHERE`(metadata 컬럼 `= != < <= > >=` 6연산자, `pack_id` 등가는 partition key 로). **localcrab 의 현 필터는 `pack_id` 등가가 지배적**(builder meta = pack_id/source/node_id)이라 vec0 로 충분하나, **임의 중첩 필터는 미지원**(PG JSONB 대비 한계, §10).
- **인덱스/거리:** cosine(`distance_metric=cosine`, Chroma `hnsw:space=cosine` 정합 — 정확 문법 구현 시 재확인). KNN 은 **브루트포스 full-scan**이 stable 경로 — ANN(IVF/DiskANN)은 소스에 존재하나 **stable 지원 미확정**이므로 브루트포스로 설계하고, **1024d × 전체 청크 지연을 §11.1 벤치 게이트로 심판**한다.
- **동시성:** vec0 shadow table 은 일반 SQLite 테이블 → **SQLite WAL 상속**. `LocalSQLDocStore`(`local_sql_doc_store.py:98-178`)의 **스레드-로컬 커넥션 + `self._lock` write 락 + `PRAGMA journal_mode=WAL/synchronous=NORMAL` + `_all_conns` 생명주기 패턴을 그대로 차용**한다. 배치 선택: 전용 `vectors.db` 로 분리(스토어 독립성) 또는 `doc_store.db` 에 편입(단일 파일 백업 정합). **다중 프로세스 읽기 동시 + 라이터 직렬화** — 현행 로더 stop-to-load 와 정합(§9).
- **가용성/폴백:** sqlite-vec 확장 로드 실패 시 `available=False`, 쓰기 `RuntimeError`, `count()→0` — §3.4 가드 동일 재현.

### 3.7 성능 최적화 — binary 2단계 양자화 (전역 검색용, 후속 과제)

**동기(실측 2026-07-01, `scripts/qa/bench_vector_backend.py`).** 라이브 KURE 컬렉션 **179,622 벡터(1024d)** 기준:

| 지표 | 결과 | 게이트 |
|------|------|--------|
| pack-filtered(partition) p95 | **8.3ms** | ≤200ms ✅ |
| 전역(no pack_ids) 브루트포스 p95 | **868ms** | ≤100ms ❌ |
| recall@10 vs Chroma | 0.925 | ≥0.95 (참고: sqlite-vec=exact가 정답, Chroma HNSW=근사라 놓침 → **정확도는 sqlite-vec 우위**) |
| pack isolation leak | 0 | 0 ✅ |
| vec0 build / size | 135s / 1.06GB | — |

→ **pack-scoped 검색은 Chroma보다 빠르나(8ms), 전역(pack 미지정·`include_unpackaged=True`) 검색이 179k×1024d 브루트포스라 868ms.** 이는 **CPU/메모리대역폭 바운드**(쿼리마다 735MB 스트리밍 + 184M cosine 연산)라 **페이지캐시 웜에서도 동일** — OS 캐시는 콜드 첫 쿼리의 디스크읽기(~0.5s)만 개선. 연산량 자체를 줄여야 한다.

**무엇을 양자화하나.** 임베딩 벡터(1024 float32)를 **부호 1bit로 압축한 binary 벡터 `bit[1024]`(128B)**를 float 원본과 **함께** 저장한다. 데이터·메타데이터 불변, 압축 사본만 추가(+~23MB/179k). 정밀 float은 리랭크용으로 유지.

**2단계 검색(전역을 ~30ms로).**
1. **coarse(bit Hamming):** `qbit=sign(qvec)` 로 bit 벡터 KNN → 전체 179k에서 후보 C개(예 256~512) 추림. XOR+popcount라 float 대비 ~32× 빠름(~20ms).
2. **rerank(float cosine):** 후보 C개의 원본 float 벡터만 qvec와 cosine 재정렬 → top-n. C개뿐이라 <5ms.
- 결과 전역 ~30ms 목표. recall은 **C로 튜닝**(C↑ → exact 근접). where post-filter는 후보 단계에서 적용, partition pushdown은 coarse 단계에 동일 적용.

**스키마(vec0).** 한 테이블에 두 벡터 컬럼(가능 여부 착수 시 확인; 불가 시 bit 전용 보조 테이블 + `node_id` 조인):
```sql
CREATE VIRTUAL TABLE vectors_kure USING vec0(
  node_id TEXT PRIMARY KEY,
  pack_id TEXT partition key,
  embedding     float[1024] distance_metric=cosine,   -- rerank(정밀)
  embedding_bit bit[1024]   distance_metric=hamming,   -- coarse(고속)
  +document TEXT, +metadata TEXT
);
```

**bit 파생 = 비파괴 마이그레이션.** float 원본의 **부호 비트만** 추출해 채운다 — **재임베딩 불필요**. 기존 `vectors.db`에 컬럼/보조테이블 추가 후 일괄 backfill(`bit = pack_sign_bits(float_vec)`). 따라서 **지금 float로 전환한 뒤 나중에 언제든 비파괴로 얹을 수 있다.**

**recall 검증(필수 게이트).** binary 2단계 top-10 vs exact float top-10 overlap **≥0.95** 되도록 C 튜닝. `bench_vector_backend.py`에 binary 모드 추가해 재측정. (참고: int8[1024](1B/dim)은 ~200ms로 중간 옵션이나 100ms 미달이라 단독 부적합 → **binary 2단계 채택**.)

**구현 위치.** `SqliteVecStore`(2단계 query 경로 + bit 저장/파생), `config`(예 `VECTOR_ANN=binary` 토글, 기본 off), 마이그레이션(bit backfill), 테스트(2단계 recall·parity), bench(binary 측정).

**리스크.** sqlite-vec 의 `bit`/`hamming`·다중 벡터컬럼은 pre-v1이라 착수 시 최소예제 검증 필수. recall은 C 의존 → 튜닝·게이트 없이 채택 금지. **전역 검색 빈도가 낮으면 이 최적화는 선택**(현 float로도 pack-scoped는 8ms로 충분, 전역만 ~0.9s).

> **현 결정(2026-07-01):** 전역 지연을 수용하고 **float로 먼저 라이브 전환**(정확도↑·동시성 회복). binary 2단계는 위 설계대로 **후속 비파괴 확장**으로 보류.

---

## 4. 스키마 설계

### 4.1 vectors 테이블

**(B) PostgreSQL + pgvector:**

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

**(A) sqlite-vec + vec0 (SQLite-unified):**

```sql
-- sqlite-vec 확장 로드 후
CREATE VIRTUAL TABLE vectors_kure USING vec0(
    node_id   TEXT PRIMARY KEY,                 -- ChromaStore와 동일 16자 sha256 ID
    pack_id   TEXT PARTITION KEY,               -- pack isolation = 검색 사전필터(샤딩)
    embedding float[1024] distance_metric=cosine,
    +document TEXT,                             -- auxiliary(SELECT 반환 전용, WHERE 불가)
    +metadata TEXT                              -- auxiliary(JSON: node_id/source 등)
);
```

- ⚠️ **정확 문법은 구현 착수 시 sqlite-vec 문서로 재확인**(auxiliary `+` 접두, `PARTITION KEY`, `distance_metric` 표기·cosine 정규화 요건, TEXT PK 지원 버전).
- vec0 제약: **metadata 컬럼 최대 16개**(`= != < <= > >=` 6연산자), **partition key 최대 4개**(1개 초과 시 과샤딩 위험), auxiliary 최대 16개. localcrab 의 `pack_id` 등가필터엔 충분(§3.6·§10).
- 규모 성장 시 ANN: sqlite-vec 의 IVF/DiskANN 은 **stable 미확정** → 우선 브루트포스, 지연 초과 시 §11.1 게이트로 재평가.

`metadata` 는 Chroma가 string/int/float/bool 만 허용해 `_sanitize_metadata` 로 평탄화하던 제약이 사라진다. JSONB는 중첩 구조를 그대로 저장 가능하므로, 호환을 위해 당분간 동일 형태를 유지하되 향후 풍부한 메타데이터 저장이 가능하다.

### 4.2 doc / sql 테이블 매핑

- **sql**: `SQLStore._TABLES_SQL`(Postgres DDL)이 이미 존재 — `ontology_nodes`, `ontology_edges`, `impact_records`, `lever_simulations`, `rebac_policies`. 추가 작업 없이 URL만 Postgres로.
  - ⚠️ **`ontology_nodes` 는 노드 본문이 아니라 얇은 레지스트리다.** `register_node(space, node_type, node_id)`(`builder.py:139`)로 **키 3개만** 저장하며 properties 컬럼이 없다. 노드 본문(properties JSON)은 `graph_nodes`(graph)·`doc_nodes`(doc)에 **중복 저장**된다. 따라서 단일-DB 이전은 이 3중 구조를 **각각 1:1 이전(parity-safe, §8.0)** 하고, 단일 canonical 노드 테이블로의 dedup 은 후속(§13)으로 둔다.
- **doc**: `LocalSQLDocStore` 의 3테이블(`doc_nodes`(PK `space,node_id`), `doc_sources`, `audit_log`)을 Postgres 동등 테이블로 옮긴다. `properties`/`metadata`/`details` 는 SQLite에서 JSON TEXT였으나 Postgres에서는 **JSONB**로 승격 권장(검색·필터·FTS 용이).

### 4.3 차원(N) 관리 — KURE 1024 고정

- pgvector 표준은 **KURE(`EMBED_DIM=1024`) 단일**이므로 PG 에는 `opencrab_vectors_kure vector(1024)` **한 테이블만 생성**한다. pgvector 의 `vector(N)` 차원 고정 제약을 KURE 단일화로 자연스럽게 해소.
- **minilm(384d) 테이블은 PG 에 만들지 않는다.** minilm 은 롤백용 Chroma 컬렉션(`opencrab_vectors`)에만 존재(§10). 향후 다른 차원 모델을 추가하면 그때 별도 테이블(`opencrab_vectors_<model>`)로 분리하는 전략을 따른다.
- **(A) 동일:** vec0 `embedding float[1024]` 로 KURE 1024 단일 고정을 자연 해소. minilm 은 (A)에서도 롤백용 Chroma 에만 잔존, vec0 에는 KURE 테이블(`vectors_kure`) 하나만 만든다.

---

## 5. doc / sql 이전

> **(B) PG-unified 한정.** (A) SQLite-unified 에서는 doc/sql 이 SQLite 그대로라 이 절 전체가 불필요하다(벡터만 교체).

- **sql**: `make_sql_store`(`factory.py:162-167`) 가 로컬에서 `settings.sqlite_url` 을 쓰는 분기를, 신규 플래그(`STORE_BACKEND=pgvector`)로 분기. `SQLStore(url=settings.postgres_url)` 만으로 동작(Postgres DDL 이미 보유).
- **doc**: `LocalSQLDocStore`(`local_sql_doc_store.py`) 와 동일 인터페이스를 갖는 `PgDocStore` 신규 작성(시그니처는 `MongoStore`/`LocalSQLDocStore` 호환 — `list_nodes(limit=...)`, `upsert_node_doc`, `get_node_doc`, sources, `log_event`/`get_audit_log`). `doc_nodes`(PK `space,node_id`)/`doc_sources`/`audit_log` 의 `properties`/`metadata`/`details` 는 JSON TEXT → **JSONB**. `list_nodes(limit=50000)` 은 BM25 재빌드 핫패스이므로(§7) Postgres에서도 인덱스된 `LIMIT` 스캔으로 O(k) 보장.
- **엔진 공유**: `PgDocStore`·`SQLStore`·`PgVectorStore` 모두 factory 가 1회 생성한 **동일 SQLAlchemy 엔진**을 주입받는다(§3.5). 단일 풀 + 교차 트랜잭션.
- 데이터 이전: SQLite → Postgres 로우 단위 복사(§8). JSON TEXT 컬럼은 `::jsonb` 캐스트.

---

## 6. graph 처리 — PG 그래프/Cypher 플러그인 평가

> **⚠️ 이 절 전체는 (B) PG-unified 한정.** **(A) SQLite-unified 에서는 graph 가 무변경**이다 — sqlite-vec 는 벡터만 교체하므로 `LocalGraphStore`(SQLite 인접테이블 + 파이썬 BFS)가 그대로 유지되고, 아래 §6.2~§6.4 의 **AGE vs 재귀 CTE 재구현·PoC·16메서드 동등성 검증이 통째로 불필요**하다. 이것이 (A)의 최대 이점(비용 절감)이다(§1·§2). (B)를 택할 때만 아래를 따른다.
>
> **용어 주의:** 이 절 **내부**의 `(A)/(B)/(C)`(§6.1·§6.3)는 **graph 백엔드 옵션**(A=Apache AGE, B=재귀 CTE, C=SQLite graph 유지)을 가리키며, **문서 수준의 (A)SQLite-unified/(B)PG-unified 와 다른 축**이다.

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

### 6.4 Canonical store + AGE projection 원칙 (외부 검토 반영)

외부 검토(Codex)가 제기한 안전 원칙을 채택한다: **graph 의 source of truth 는 항상 plain PG 테이블**(`graph_nodes`/`ontology_*` + §6.1 의 노드/엣지)이고, **AGE 는 그 위에 sync 하는 optional projection layer** 로 둔다. 이렇게 하면 AGE 장애·버전 문제가 나도 core 데이터·기능(재귀 CTE 경로)은 살아 있다.

- **canonical(항상 존재):** plain PG 노드/엣지 테이블 + **재귀 CTE**(B)로 1-hop/N-hop/reverse·pack isolation·delete cascade 동작. 디버깅·migration·백업 검증이 SQL 로 투명.
- **projection(선택, AGE 우선 후보):** canonical → AGE 그래프로 1방향 sync. Cypher 표현력/성능이 CTE 대비 명확히 이득일 때 채택.
  - 사용자 지침에 따라 **AGE 를 우선 후보**로 평가하되(§6.3 PoC 게이트), canonical-CTE 가 항상 1차 안정 경로임을 병기. 둘 다 문서화하고 **Phase 4 벤치마크(§11)** 결과로 최종 선택(§8).

```sql
-- canonical 재귀 CTE traversal (확장 0, 결정론적)
WITH RECURSIVE walk AS (
  SELECT e.pack_id, e.from_space, e.from_id, e.relation, e.to_space, e.to_id,
         1 AS depth, ARRAY[e.from_space||':'||e.from_id] AS path
  FROM ontology_edges e
  WHERE e.pack_id = :pack AND e.from_space = :sp AND e.from_id = :id
  UNION ALL
  SELECT e.pack_id, e.from_space, e.from_id, e.relation, e.to_space, e.to_id,
         w.depth+1, w.path || (e.from_space||':'||e.from_id)
  FROM ontology_edges e
  JOIN walk w ON e.pack_id=w.pack_id AND e.from_space=w.to_space AND e.from_id=w.to_id
  WHERE w.depth < 4 AND NOT (e.from_space||':'||e.from_id = ANY(w.path))  -- 사이클 가드
)
SELECT * FROM walk;
```

```sql
-- AGE projection (옵션): canonical 에서 sync. source of truth 아님.
CREATE EXTENSION IF NOT EXISTS age;  LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('localcrab');
SELECT * FROM cypher('localcrab', $$
  MATCH (a {node_id:'concept:rag'}), (b {node_id:'concept:pgvector'})
  CREATE (a)-[:DEPENDS_ON {pack_id:'demo'}]->(b)
$$) AS (e agtype);
```

---

## 7. BM25 vs Postgres FTS (선택)

- 현재: 파이썬 인메모리 `BM25Index`(`opencrab/ontology/bm25.py`). 한국어를 위해 `_tokenize` 가 Hangul 2/3-gram을 추가 생성하는 커스텀 토크나이저를 쓴다. **2026-06 개선(`feat/bm25-bg-rebuild`)**: 재빌드를 쿼리 hot path에서 분리 — `invalidate_bm25_cache()`가 백그라운드 워커를 디바운스(`OPENCRAB_BM25_DEBOUNCE`) 트리거하고 완료 시 원자적 swap, 쿼리는 경량 `doc_store.bm25_fingerprint()`(`COUNT(*), MAX(updated_at)` LIMIT N, 행 파싱 없음)만 확인해 out-of-band 쓰기를 감지(상세: `docs/ARCHITECTURE.md` 핫패스 §). `list_nodes(limit=50000)` 풀스캔/동기 재빌드가 hot path에서 제거됨.
- 대안(최종 종착): Postgres `tsvector`/`tsquery` FTS. `doc_nodes` 에 `tsvector` 생성 컬럼 + GIN 인덱스를 두면 **재빌드·콜드빌드·out-of-band 정합성이 DB 네이티브로 해소**(증분 동기, 다중 프로세스 공유) — 위 백그라운드 재빌드도 불필요해진다.
- **주의:** Postgres 기본 FTS(`unicode61`/`trigram`)는 한국어 2-gram 미지원 → `pg_bigm`/`pgroonga` 확장 필요(aarch64 빌드 부담). tsvector 흡수 시 선행 과제: ① Hangul n-gram 재현, ② `_node_text`의 `_TEXT_FIELDS` 필드가중을 다중 컬럼 + `ts_rank`/`bm25()` weight로 재설계, ③ `matches_pack_filter`/`infer_pack_id` pack·space 필터를 SQL where로 재현, ④ doc_nodes에 GIN 인덱스 신규 빌드(대량). **relevance 회귀 검증(BM25 결과 동등성·한국어 MRR) 필수.**
- **권장:** tsvector 흡수는 이번 마이그레이션 범위에서 **제외**(개선된 인메모리 BM25 유지). 별도 후속 과제로 분리하되, 위 ①~④ 설계를 본 과제 정의로 삼는다. `list_nodes`/`bm25_fingerprint` 가 Postgres(`PgDocStore`)로 옮겨가도 인터페이스 동일하므로 BM25 백그라운드 재빌드는 무변경으로 계속 동작한다.

### 7.1 키워드 FTS 레그 (구현됨, 2026-06) — `feat/hybrid-fts-keyword`
- BM25(노드 필드 색인)는 **청크 본문(`doc_sources.text`)을 색인하지 않아** 본문 속 약어·표준번호(JASO M345, FB/FC)·영어 다중어 질의가 전역 검색에서 밀리는 문제가 있었다.
- 해결: doc store에 **백엔드-중립 capability** 추가 — `supports_keyword: bool` + `keyword_search(query, pack_ids, include_unpackaged, limit)`. `HybridQuery._fts_search()`가 capability 보유 시에만 호출(미지원·예외 시 graceful 폴백)하고 기존 `Reranker.rerank()`(RRF)로 융합. reranker source 가중치 `keyword`(>bm25).
- **LocalSQLDocStore**: SQLite **FTS5** 가상테이블 `doc_sources_fts`(`tokenize='unicode61'`, 한+영) — `_init_db`에서 생성 + idempotent 마이그레이션, `upsert_source`에서 동기화. 질의는 `\w+` 토큰을 따옴표 OR 결합(연산자 주입 방지).
- **(A) sqlite-vec 이전 시**: doc store 가 SQLite 그대로이므로 **FTS5(`doc_sources_fts`)는 무변경 유지** — 벡터·키워드가 모두 SQLite 로 일관되고 tsvector 전환 과제 자체가 없다.
- **(B) Postgres(pgvector) 이전 시**: `PgDocStore`가 동일 capability를 `tsvector`(또는 `pg_bigm`/`pgroonga`) + GIN으로 구현하면 `HybridQuery`는 무변경. 미구현이면 `supports_keyword=False`로 두어 자동 폴백.
- **한계(알려짐):** RRF는 다중 리트리버 동시 등장 항목을 우대하므로, 키워드 단독 매칭은 의미충돌(예 "smoke" 색상 청크)이 vector+graph로 강하게 잡힐 때 top-3 밖으로 밀릴 수 있다. pack 스코프 질의에선 정확. 깊은 RRF 단독소스 보정은 후속 과제.

---

## 8. 마이그레이션 절차

### 8.0 단계적 경로 (외부 검토 반영 — 위험 최소화)

각 단계는 **직전 단계 green 유지**를 전제로 진행한다(§11). 한 번에 모두 바꾸지 않는다.

**(A) SQLite-unified(sqlite-vec) 경로 — 짧음(graph/doc/sql/FTS5 무변경):**

| Phase | 상태 | 내용 |
|-------|------|------|
| 0 | 현행 동결·측정 | (공통 기준선) Chroma 컬렉션·차원·pack별 vector count·대표 질의 20개·삭제 시나리오 측정 |
| A1 | **SQLite + sqlite-vec** | `SqliteVecStore`(§3.6) 구현 + KURE 전량 재임베딩 → vec0 적재. **graph/doc/sql/FTS5 무변경** |
| A2 | **Chroma 제거** | Chroma flock 층·로더-serve 상호배제 규율 제거, 벡터를 SQLite 스레드-로컬+락+WAL 규율에 편입, `test_store_concurrency.py` 확장 |
| A3 | **게이트** | §11.1 벤치(브루트포스 1024d 지연·partition 필터·recall) 통과 → 채택 / 미달 시 (B) 재평가 |

**(B) PG-unified(pgvector) 경로 — 전면(아래 상세):**

| Phase | 상태 | 내용 |
|-------|------|------|
| 0 | 현행 동결·측정 | SQLite 스키마/Chroma 컬렉션·차원·pack별 node/edge/vector count·대표 질의 20개·삭제 시나리오 측정(벤치 기준선) |
| 1 | **PG + Chroma** | sql/doc 메타데이터를 PG 로 먼저 이전, **벡터는 Chroma 유지**. 앱 core 를 PG 기준으로 전환 |
| 2 | **PG + pgvector** | Chroma → `opencrab_vectors_kure`(KURE 재임베딩). 동일 질의 Chroma vs pgvector top-k(recall@10·latency·pack 필터) 비교 |
| 3 | **PG canonical graph(CTE)** | graph 를 plain PG 테이블 + 재귀 CTE 로 안정화(1-hop/N-hop/reverse·pack isolation·delete cascade) |
| 4 | **AGE projection PoC** | canonical → AGE sync(§6.4). Cypher 표현력/성능/ sync 비용/장애 격리 검증 |
| 5 | **최종 선택** | 벤치 결과로 AGE 채택 vs CTE 만 유지 결정 |

> **노드 중복은 dedup 하지 않는다(parity-safe).** 현행 `graph_nodes`/`doc_nodes`/`ontology_nodes`(레지스트리) 3중 구조를 **각각 PG 테이블로 1:1 이전**해 스토어 계약·green→green parity 를 보존한다. 단일 canonical 노드 테이블로의 dedup 은 별도 후속 최적화(§13).
> **설치는 RPi5 aarch64 네이티브 소스 빌드**(pgvector·AGE). Docker 이미지/Compose 는 docker 모드 한정이며 로컬 모드와 무관.

### 8.1 상세 절차

> **(A) sqlite-vec 경로는 아래 1~3(Postgres 설치·롤 생성·공유 엔진 부트스트랩)·6(doc/sql 덤프)을 건너뛴다.** 대신 *① sqlite-vec 확장 로드(`sqlite3` 확장 or Python `sqlite_vec.load`) → ② vec0 DDL(§4.1-A) → ③ KURE 전량 재임베딩·적재(아래 5와 동일) → ④ factory 분기(아래 7) → ⑤ 검증(아래 8)* 만 수행. doc/sql/graph 는 SQLite 그대로라 이전 불필요. 아래는 (B) PG 경로 상세.

1. **PostgreSQL + pgvector 설치 (aarch64/RPi5, 네이티브).**
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

## 9. 동시성 결론 — 의사결정 힌지

두 옵션은 Chroma 약한 고리를 **서로 다른 강도로** 해소한다. **이것이 (A)/(B)를 가르는 단일 힌지다.**

- **(B) PG-unified — 근본 해소.** PostgreSQL MVCC로 **MCP 서버 + 백그라운드 로더(또는 다중 클라이언트)가 락 없이 동시 가동**된다. 리더는 라이터를 막지 않고, 라이터는 행 단위로만 경합한다. `[[ingestion-via-mcp-plan]]` 이 요구하던 "단일 라이터 직렬화" **우회 자체가 불필요**해진다(Chroma 단일프로세스 제약과 SQLite 라이터 직렬화가 모두 해소).
- **(A) SQLite-unified — 규율 통일(MVCC 아님).** sqlite-vec 는 MVCC를 주지 **않는다**. 벡터를 나머지 3스토어와 **같은 SQLite WAL 규율(다중 프로세스 읽기 + 라이터 직렬화)**로 통일할 뿐이다. Chroma 의 "다중 프로세스 동시 쓰기 불가"·자작 flock 층은 사라지지만, **라이터는 여전히 직렬화**된다.

**결정 힌지 = "진짜 다중 라이터가 확정 요구인가?"**

- **아니오(현행 stop-to-load).** 로더는 적재 시 어차피 MCP 를 정지하므로 **사실상 단일 라이터**이고 워크로드는 읽기 지배다. 이 현실에선 (B)의 MVCC 는 **미사용 자산**이고, **(A) sqlite-vec 의 라이터 직렬화로 충분**하다 — Postgres 데몬·AGE·graph 재구현(§6) 비용 없이 Chroma 통증만 정확히 제거. (A)는 이 직렬화를 우회가 아니라 SQLite 규율로 **정식 편입**한다.
- **예(실시간 동시 적재 확정).** `[[ingestion-via-mcp-plan]]` 처럼 로더가 MCP 서빙 중에 동시 write 해야 하거나, 다중 serve 가 동시 write 하거나, 벡터가 수백만 스케일이면 **(B)의 MVCC 만이 정답**이고 (A)는 구조적 막다른 길이다.

> 요약: **(A)는 동시성을 "충분히" 해소(현행 요구 기준), (B)는 "근본" 해소(미래 다중 라이터 대비).** 그 차이의 값을 지불할 이유가 있는지가 선택 기준이다(§2.1).

---

## 10. 리스크 / 롤백

**리스크**

- **재임베딩 비용.** 전 벡터를 1회 KURE로 재생성해야 함(임베딩 서버/GGUF 부하). 데이터 규모에 비례한 시간·CPU 소요.
- **RPi5 Postgres 부담.** 상시 서버 프로세스 + `shared_buffers`/`work_mem`/`maintenance_work_mem` 메모리, HNSW 인덱스 빌드 시 CPU·메모리 스파이크. 인프로세스 SQLite/Chroma 대비 상시 자원 점유 증가. → `shared_buffers` 보수적 설정, 인덱스는 규모 따라 단계적 도입으로 완화.
- **pgvector aarch64 빌드.** 배포판 패키지 부재 시 소스 빌드 필요.
- **Apache AGE aarch64 빌드/버전 결속(graph 옵션 A 채택 시).** 배포 패키지 없음 → 소스 빌드 + PG 메이저 버전(11–18) 헤더 일치 필요. PoC 게이트(§11)를 통과 못 하면 재귀 CTE(B)로 폴백.
- **graph 옵션(B) 채택 시** 재귀 CTE 재구현·성능 검증 리스크(허브 LIMIT·pack 3규칙·결정론 재현).

> 위 4개 항목 대부분은 **(B) PG-unified 전용**(Postgres·pgvector·AGE·CTE). **재임베딩 비용은 (A)(B) 공통**(Chroma 내부벡터 이전 불가).

**(A) SQLite-unified(sqlite-vec) 리스크:**

- **pre-v1 성숙도.** sqlite-vec 는 v0.1.x pre-v1 로 **파괴적 변경이 예고**돼 있다. Chroma 손상 이력이 있는 스택이 장기 표준으로 삼기 전 버전 고정·회귀테스트 필수.
- **브루트포스 지연.** stable KNN 은 full-scan → **1024d × 전체 청크** 지연이 규모에 비례. ANN(IVF/DiskANN) stable 미확정 → §11.1 벤치가 채택 게이트.
- **metadata 필터 한계.** vec0 는 metadata 16컬럼 + 6연산자(`= != < <= > >=`), partition key 4개. localcrab 의 `pack_id` 등가필터엔 충분하나 **PG JSONB 임의 중첩쿼리 대비 표현력 낮음** — 향후 복합 필터 요구 시 제약.
- **vec0 문법·기능 변동.** distance_metric·partition/auxiliary·TEXT PK 지원이 버전별로 다를 수 있어 구현 착수 시 재확인 필요(§3.6·§4.1-A).

> minilm 직접임베딩 리스크는 **KURE 단일 표준화로 해소**(§3.2) — sqlite-vec/pgvector 어느 쪽도 minilm 을 재구현하지 않는다.

**롤백 경로**

- **기존 SQLite 파일과 Chroma 디렉터리를 보존**한다(삭제 금지). factory 분기 기본값을 기존(local)으로 두면, `STORE_BACKEND`/`EMBEDDING_BACKEND` 등 **설정만 되돌려** 즉시 기존 스택으로 복귀 가능. minilm(384d)은 롤백용 Chroma 컬렉션(`opencrab_vectors`)에 그대로 남아 있고, PG 의 `opencrab_vectors_kure`(1024d)는 별도 테이블이라 원복이 비파괴적이다.
- **(A)(B) 공통.** 롤백 메커니즘은 동일하다 — Chroma 보존 + `STORE_BACKEND` 되돌림. (A)는 vec0 테이블(`vectors_kure`)이 별도 SQLite 객체라, (B)는 PG 테이블이 별도라 각각 원복이 비파괴적이다.

---

## 11. 테스트 전략 — 회귀 검증(green→green, 특성화/parity)

이건 새 동작을 만드는 TDD(red→green)가 **아니다**. 기존 동작 보존을 검증하는 마이그레이션이므로 **green→green**: 테스트를 **현행 SQLite/Chroma 구현에 대해 먼저 통과(green)** 시켜 기존 동작을 골든으로 고정 → PG 백엔드 구현 → **동일 테스트가 백엔드만 바뀐 채 그대로 통과(green)**. 어느 단계에서도 red 가 나면 안 된다(red = 회귀).

- **필수 순서:** ① 현행 백엔드로 특성화 테스트 작성·실행 → **green(베이스라인 고정)** → ② 신규 백엔드 구현 → ③ **백엔드 파라미터라이즈**(`local`/`sqlite-vec`/`pg`)로 같은 스위트 재실행 → **green(parity 확인)**. **(A) sqlite-vec 와 (B) pgvector 는 같은 ChromaStore 인터페이스 parity 스위트를 공유**하므로 동일 게이트로 나란히 검증된다.
- **정상(happy):** `add_texts`/`upsert_texts` 후 `query` top-k, `count()` 정합, `get_by_id`, doc `upsert_node_doc`/`list_nodes(limit)` O(k), graph `find_neighbors`/`find_path` 기대 이웃 — **두 백엔드 동일 결과**.
- **실패(failure):** 미가용 시 `available=False`+쓰기 `RuntimeError`(`count()`→0) — ChromaStore 가드를 **PG 도 동일 재현**. `vector(1024)` 차원 불일치 INSERT 거부, EF 예외 시 `ResilientEmbeddingFunction` fallback 경로.
- **엣지(edge):** 빈 입력(`[]`→`[]`), 중복 ID upsert 멱등, `where` 필터(중첩/없음) — Chroma where ↔ JSONB 번역 동치, 한국어 텍스트, 허브 노드 차수>limit 슬롯 동작, pack 필터 3규칙(외부 pack 노드/엣지 배제, `include_unpackaged` 분기), 사이클 그래프 — **현행 동작과 동치**.
- **회귀 기준선(green 유지 대상):** KURE 한국어 top-k **MRR(현행 1.000)**, Chroma↔pgvector `count()`·`table_counts()` parity, BM25 결과 동등성, graph parity(LocalGraphStore 골든 vs AGE/CTE — `find_neighbors`/`find_path`/`list_packs`, Jaccard 임계).
- **픽스처:** `tests/` 의 `local_stores` 패턴(monkeypatch `LOCAL_DATA_DIR`)을 **백엔드 파라미터라이즈** `stores(backend)`(`local` 임시 dir / `pg` 임시 스키마)로 확장. 임베딩은 **결정적 mock EF**(`__call__` 고정 벡터)로 외부 LM Studio/GGUF 의존 제거(두 백엔드 동일 벡터 → 결과 동치 비교). PG 테스트는 `@pytest.mark.skipif(no PG)`.
- **동시성 스모크:** MCP 서버 + 백그라운드 로더 동시 적재(MVCC 락 무경합) — 현행 `test_store_concurrency.py` 패턴 PG 버전.
- **pack-delete cascade 일관성(외부 검토 반영):** pack 삭제 시 해당 pack 의 node/edge/vector 가 4개 스토어(또는 PG 테이블) 전부에서 사라지고 **orphan row=0**. 현행 4중 fan-out 에서 삭제 정합은 실질 리스크이므로 회귀 차원으로 고정.
- 실행: `pytest`(`pyproject.toml:92` testpaths=tests, asyncio auto). 회귀 게이트 = **전체 스위트가 두 백엔드 모두 green**.

### 11.1 벤치마크 성공 기준 (Phase 2·4 의사결정 게이트)

AGE 채택 여부·벡터 백엔드(sqlite-vec vs pgvector) 전환 승인은 측정으로 결정한다(추측 금지). **Phase 2 에서 sqlite-vec 와 pgvector 를 아래 동일 게이트로 나란히 벤치**해 승자를 채택한다(§2.1 힌지와 함께). (A) 전용 관찰 지표: **브루트포스 1024d top-k p95**(규모별), **partition-key pack isolation(leak 0)**, **vec0 metadata 16컬럼 한계 내 필터 정확도**. 목표 임계치:

| 지표 | 목표 |
|------|------|
| single-pack vector top-k p95 | ≤ 100ms |
| metadata-filtered top-k p95 | ≤ 200ms |
| 3-hop graph traversal p95 | ≤ 100ms |
| Chroma 대비 recall@10 | ≥ 0.95 |
| pack isolation leakage | 0 (pack 간 누출 없음) |
| pack delete consistency | orphan row 0 |
| backup/restore | 1 command(또는 1 workflow)로 완전 복구 |
| cold start / disk usage | SQLite+Chroma 대비 측정·기록(악화 시 명시) |

---

## 12. 병렬 작업 방식 — 다이나믹 워크플로우

> **(A) SQLite-unified 채택 시**: 워크스트림이 **`SqliteVecStore` 단일 스트림**으로 축소된다(doc/sql/graph/FTS5 무변경, factory 분기만 추가) — 아래 (B)의 4갈래 병렬 워크플로우는 불필요. 병렬화 없이 순차 단일 PR 로 충분.

**(B) PG-unified 채택 시**, 구현은 **독립 워크스트림 4갈래**를 **병렬 다이나믹 워크플로우**로 실행한다(각자 git worktree 격리로 파일 충돌 방지):

1. `PgVectorStore` (+ KURE EF 재사용, `opencrab_vectors_kure` DDL)
2. `PgDocStore` (`LocalSQLDocStore` 인터페이스, JSONB)
3. graph 백엔드 PoC (AGE aarch64 소스 빌드 + Cypher 골든 / 폴백 재귀 CTE)
4. `factory` 분기 + 1회성 마이그레이션 스크립트 + 공유 엔진 배선(`builder.py` 4중 fan-out)

- **각 워크스트림 내부도 green→green 파이프라인**(현행 특성화 테스트 통과 → PG 구현 → 동일 테스트 parity 통과)으로 단계화. 스트림 간 의존 없는 stage 는 pipeline 으로 무배리어 진행.
- **합류 지점:** 공유 SQLAlchemy 엔진 시그니처와 `Settings` 신규 플래그(`STORE_BACKEND`)를 **먼저 합의(인터페이스 동결)** 후 분기. 최종 회귀(§11 전체 스위트)는 합류 후 일괄 게이트.
- **착수 전 세션 컴팩트** 권장(탐색·조사로 컨텍스트 누적).

---

## 13. 외부 검토(Codex) 반영 요약

동일 주제의 외부 검토(Codex)를 localcrab 실제 코드와 대조해 **채택/정정**을 정리한다. 방향(PG 단일화 + pgvector)은 일치.

> **추가(본 문서 확장):** Codex 검토는 pgvector(B)만 상정했으나, 본 문서는 §0 의 사실(약한 고리 = Chroma 하나)에서 도출되는 **(A) SQLite-unified(sqlite-vec)** 를 대등 병기했다. (A) 채택 여부는 **§9 다중 라이터 힌지 + §11.1 벤치**로 결정한다.

| 항목 | Codex 제안 | 본 문서 결정 | 근거 |
|------|-----------|--------------|------|
| AGE 위치 | optional projection, canonical=plain PG+CTE | **AGE 우선 후보로 평가하되 canonical-CTE 를 1차 안정 경로로 병기**(§6.4) | 사용자 지침(AGE 우선) + 안전성(장애 격리) 절충 |
| graph traversal | 재귀 CTE | **CTE 를 canonical 경로로 채택**(§6.4 예시) | 확장 0·결정론·디버깅 용이 |
| phasing | 5단계(PG+Chroma 중간) | **채택**(§8.0) | 단계별 green 유지로 위험 최소화 |
| 벤치마크 임계치 | p95·recall·orphan=0 등 | **채택**(§11.1) | 측정 기반 의사결정 게이트 |
| 노드 모델 | 단일 canonical `ontology_nodes`(properties 보유) | **반려 — parity-safe 다중 테이블 1:1 이전**(§8.0) | **`ontology_nodes` 는 실제로 키만 가진 얇은 레지스트리**(`sql_store.py:22`, `builder.py:139` 3인자); props 는 `graph_nodes`/`doc_nodes` 에 중복 저장. dedup 은 스토어 계약 대규모 리팩터 → 후속 |
| chunk 모델 | `documents`/`text_units`/`text_embeddings` 정규화 | **범위 외** | localcrab 은 node-centric, 청크 미구현(`TextUnit`=문서 단위 1노드, `tools.py:1332-1358`); `pack_ingest_chunks` 미구현(`[[ingestion-via-mcp-plan]]`). 벡터 ID 는 node_id 유지 |
| 벡터 차원 | 예시 1536 | **KURE 1024 고정**(§4.3) | 본 스택 표준 |
| Docker 이미지/Compose | pgvector+AGE 이미지 | **docker 모드 한정**, 로컬은 aarch64 네이티브 빌드(§8.0) | 로컬 모드는 Docker 미사용 |
| 벡터 백엔드 선택지 | (Codex 미상정) | **sqlite-vec(A) 를 pgvector(B) 와 대등 병기** | 약한 고리 = Chroma 하나뿐(§0), (A)는 인프라 0·graph 무변경. 선택은 §9 힌지·§11.1 벤치 |

**후속 분리 과제(범위 외):** ① 노드 3중 중복 dedup(단일 canonical 노드 테이블), ② chunk-level 저장(`pack_ingest_chunks`) — "청크 누락/적재 누락 재점검" 과제와 묶임.
