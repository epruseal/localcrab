# 벡터 백엔드 매트릭스

`STORAGE_MODE` × `VECTOR_BACKEND` × `EMBEDDING_BACKEND` 세 축의 조합과 각 백엔드의
장단점을 정리한다. 개별 축의 설정법은 [README](../README.md#임베딩-백엔드),
[README 벡터 스토어 섹션](../README.md#벡터-스토어-백엔드-vector_backend),
[ARCHITECTURE.md §8](./ARCHITECTURE.md), 설계 배경은
[pgvector-migration-plan.md](./pgvector-migration-plan.md) 참고.

---

## 1. 세 축

| 축 | 값 | 의미 |
|----|----|------|
| `STORAGE_MODE` | `local`(기본) / `kuzu` / `docker` / `pg` | 그래프·문서·SQL 스토어 위치. `local`은 SQLite 그래프를 쓰고 `kuzu`는 문서·SQL·벡터만 local로 선택하며 그래프는 capability-negative facade를 반환한다. `docker`는 Neo4j/MongoDB/PostgreSQL 외부 서비스, `pg`는 4스토어 전부 PostgreSQL(별도 분기, `is_local=False`)이다. |
| `VECTOR_BACKEND` | 미설정(조건부) / `chroma` / `sqlite-vec` / `pgvector` | 벡터를 저장·검색하는 백엔드. 임베딩 축과 독립 |
| `EMBEDDING_BACKEND` | `openai`(기본) / `local` | 텍스트를 벡터로 바꾸는 방식. 벡터 백엔드 축과 독립 |

> **운영 권장**: `local`(SQLite 단일 규율)이 기본 권장이며, `docker`(Neo4j+MongoDB+PostgreSQL+Chroma 4종 혼합)는 다중 테넌트 등 SaaS 규모 전제가 아니면 4종 스토어 관리 비용이 개별 이점을 상회해 비권장이다. 실시간 동시 write(MCP 서빙 중 백그라운드 로더) 또는 벡터 수백만 스케일이 확정 요구이면 `pg`(PostgreSQL 단일 통합, MVCC 다중 라이터)로 이행한다 — §9 힌지 참고.

---

## 2. 기본값 해석 규칙

`VECTOR_BACKEND`를 명시하지 않으면(`.env`에 값 없음) 다음 조건부 규칙으로 결정된다
(`opencrab/config.py`의 `Settings.vector_backend_resolved`):

```
VECTOR_BACKEND 명시됨?
  → 예: 그 값을 그대로 사용(항상 최우선)
  → 아니오:
      STORAGE_MODE == "pg"
        → "pgvector"
      is_local(STORAGE_MODE in {local, kuzu}) AND EMBEDDING_BACKEND == "openai"
        → "sqlite-vec"
      그 외 (STORAGE_MODE == "docker" 이거나 EMBEDDING_BACKEND == "local")
        → "chroma"
```

`EMBEDDING_BACKEND` 기본값이 `openai`이므로, **로컬 모드를 아무 설정 없이 그대로 실행하면
`sqlite-vec`가 기본으로 선택된다.** 이것이 신규 기본 동작이다.

**명시 조합 중 금지되는 하나:** `VECTOR_BACKEND=sqlite-vec` + `EMBEDDING_BACKEND=local`을
동시에 명시하면 `opencrab/stores/factory.py`의 `make_vector_store`가 기동 시
`ValueError`를 던진다 — sqlite-vec는 앱측에서 직접 임베딩해야 하는데 minilm(384d)은
sqlite-vec 표준 차원(KURE 1024d)과 맞지 않기 때문이다. sqlite-vec를 쓰려면 반드시
`EMBEDDING_BACKEND=openai`여야 한다.

---

## 3. 조합 매트릭스

| STORAGE_MODE | EMBEDDING_BACKEND | VECTOR_BACKEND(명시 안 함) | 해석 결과 | 비고 |
|---|---|---|---|---|
| `local`/`kuzu` | `openai`(기본) | _(미설정)_ | **`sqlite-vec`** | 아무 설정 없이 local 축을 실행할 때 기본 경로이며 `kuzu`의 그래프 capability를 활성화하지 않는다. |
| `local`/`kuzu` | `local` | _(미설정)_ | `chroma` | minilm 384d, ChromaDB PersistentClient |
| `docker` | `openai` | _(미설정)_ | `chroma` | docker 모드는 항상 Chroma(HttpClient) — VECTOR_BACKEND 조건에 `is_local` 필요 |
| `docker` | `local` | _(미설정)_ | `chroma` | 동일 |

| STORAGE_MODE | EMBEDDING_BACKEND | VECTOR_BACKEND(명시) | 결과 | 가능 여부 |
|---|---|---|---|---|
| `local`/`kuzu` | `openai` | `sqlite-vec` | 사용 | 가능(기본과 동일, 명시해도 무방) |
| `local`/`kuzu` | `openai` | `chroma` | 사용 | 가능(명시 롤백) |
| `local`/`kuzu` | `local` | `sqlite-vec` | **기동 실패** | **불가** — `ValueError`(minilm 384d는 sqlite-vec 미지원) |
| `local`/`kuzu` | `local` | `chroma` | 사용 | 가능(minilm 기본 조합) |
| `docker` | 무관 | `chroma` | 사용 | 가능(docker 모드 표준) |
| `docker` | 무관 | `sqlite-vec` | 사용(단, 로컬 파일 경로) | 코드상 `is_local` 체크 없이 backend 자체는 동작하나, docker 모드에서 vector만 SQLite로 로컬화하는 조합은 설계 의도 밖 — 권장하지 않음 |
| `pg` | `openai` | _(미설정)_ | **`pgvector`** | `STORAGE_MODE=pg`이면 자동 선택(4스토어 전부 PG) |
| `pg` | `local` | 무관 | **기동 실패** | **불가** — `ValueError`(minilm 384d는 pgvector 미지원, sqlite-vec와 동일 가드) |
| `local`/`kuzu`/`docker` | `openai` | `pgvector` | 사용(벡터만 PG) | 가능 — `STORAGE_MODE!=pg`여도 명시하면 벡터만 PG로 보낼 수 있음(§6.3 (C) 단계) |

---

## 4. 백엔드별 장단점

### `sqlite-vec` (로컬 모드 기본)

- **장점**
  - graph/doc/sql과 **같은 SQLite WAL 규율**로 통일 — Chroma `PersistentClient`의
    다중 프로세스 동시 쓰기 불가 제약(자작 flock 층)을 제거.
  - **무중단 ingest**: 벡터가 SQLite WAL이라 로더 쓰기와 serve 읽기가 동시 진행되고,
    라이터는 `write.lock`/SQLite `busy_timeout(5s)`로 직렬화된다. `chroma.lock(LOCK_EX)`
    선점을 위한 "적재 시 MCP 중지" 운영 절차가 불필요.
  - **pack-scoped 검색이 매우 빠르다** — 실측(179,622 KURE 벡터) p95 약 8.3ms.
  - **정확 검색(exact)**: 브루트포스 KNN이라 Chroma HNSW(근사)보다 recall이 높다.
- **단점**
  - **전역(pack 미지정) 브루트포스 검색이 느리다** — 실측 p95 약 868ms(179k×1024d,
    CPU/메모리대역폭 바운드). 전역 고속화는 **binary 2단계 양자화(`VECTOR_ANN=binary`,
    아래 §4.1)로 해결** — 구현 완료([pgvector-migration-plan.md §3.7](./pgvector-migration-plan.md)).
  - **KURE(1024d) 전용** — minilm(384d)과 조합 불가(위 §2 참고).
  - pre-v1(v0.1.x) 라이브러리 — 파괴적 변경 가능성.
  - metadata 필터는 vec0 제약(컬럼 최대 16개, `= != < <= > >=` 6연산자, partition key
    최대 4개)에 묶임 — `pack_id` 등가 필터에는 충분하나 임의 중첩 쿼리는 미지원.

#### 4.1 binary 2단계 양자화 (`VECTOR_ANN=binary`) — 전역 검색 가속

설계 원문·실측 근거: [pgvector-migration-plan.md §3.7](./pgvector-migration-plan.md).
sqlite-vec 백엔드 전용 옵트인 기능이며 **기본 off**(미설정 시 기존 exact 경로 100% 불변).

**동작 원리.** float 임베딩(1024×4B)의 **부호 1bit 사본**(`embedding_bit bit[1024]`,
128B/벡터)을 같은 vec0 테이블에 함께 저장(내구 표현·쓰기 시 동기 유지)하고,
**전역(무필터) 검색**을 인프로세스 캐시 기반 2단계로 답한다:

1. **coarse**: 쿼리 벡터의 부호 비트로 RAM bit 행렬(~23MB) hamming 스캔(numpy
   XOR+`bitwise_count`) → 후보 C개(`VECTOR_ANN_COARSE_K`, 기본 512). 179k 기준 ~16ms.
2. **rerank**: 후보 C개를 RAM int8 양자화 행렬(~184MB)로 근사 cosine 재정렬 후,
   상위 ~3n개만 vec0 point 쿼리로 **exact float cosine 재확정**(`vec_distance_cosine`)
   → 반환 distance 는 exact(콜러 계약 보존).

**왜 vec0 네이티브가 아닌 인프로세스 캐시인가(실측).** vec0 0.1.9 의 bit KNN
MATCH 스캔은 ~336ms(행당 vtab 오버헤드), 임의 point 접근은 ~0.76ms/행(접근마다
4MB 벡터 청크 실체화)이라 vec0 네이티브 2단계는 ~730ms — 게이트(≤100ms) 불달성.
캐시는 첫 전역 ANN 쿼리에서 지연 빌드(~3s, vec0 shadow 청크 직독 — vtab 풀스캔은
~97s)되고, 이 스토어의 쓰기 시 즉시 무효화 + 쿼리마다 O(1) 신선도 체크
(`{table}_rowids` max rowid, `PRAGMA data_version`)로 외부 라이터 변경을 감지해
재빌드한다. 대량 적재 중 전역 ANN 쿼리는 배치마다 재빌드(~3s)를 유발할 수 있다.

**pack-scoped 검색과 잔여 필터(where) 쿼리는 ANN을 타지 않는다** — pack 은
partition key 사전필터로 이미 ~8ms라 exact 유지(§3.7의 안전 기본), 잔여 필터는
post-filter 후보 풀을 보존하기 위해 exact 폴백. 따라서 pack isolation 특성은
ANN on/off와 무관하게 동일하다.

**스키마 감지 게이팅.** vec0는 테이블에 bit 컬럼이 있으면 모든 INSERT에 bit 값을
요구한다(NULL 불허). 따라서 쓰기 경로는 config 플래그가 아니라 **실제 테이블 스키마**
(`PRAGMA table_info`의 `embedding_bit` 존재 여부)로 게이팅된다:

- bit 컬럼 있음 → `VECTOR_ANN` 설정과 무관하게 쓰기 시 bit 동기 유지(부호비트 파생, 재임베딩 없음).
- bit 컬럼 없음 + `VECTOR_ANN=binary` → 경고 로그 후 exact 경로 폴백(안전).
  신규/빈 DB는 생성 시 bit 컬럼 자동 포함, 기존 DB는 아래 마이그레이션 필요.

**recall 튜닝.** coarse 단계가 놓친 항목은 rerank가 복구할 수 없으므로 recall은
C(`VECTOR_ANN_COARSE_K`)로 튜닝한다. 게이트: 2단계 top-10 vs exact top-10 overlap
≥0.95. 실측 수치는 아래 게이트 표 참고. C는 코퍼스 크기로 클램프된다.

**메모리 비용.** 캐시는 프로세스당 약 1.15MB/1k벡터(int8 1KB + bit 128B +
scale/ids) — 179k 기준 ~210MB. serve 다중 인스턴스는 각자 캐시를 가진다.

**게이트 실측 (179,784 KURE 1024d 실데이터 사본, `scripts/qa/bench_vector_backend.py --mode binary`):**

| 지표 | 결과 | 게이트 |
|------|------|--------|
| 전역 exact 브루트포스 p50/p95 (baseline) | 570.2 / 592.9 ms | — |
| 전역 binary 2단계 p50/p95 (채택 C=512) | **47.8 / 54.8 ms** | ≤100ms ✅ |
| recall@10 vs exact (C=512) | **0.9950** | ≥0.95 ✅ |
| recall@10 vs exact (C=256 / C=1024) | 0.9830 / 1.0000 | (참고 스윕) |
| pack isolation leak | **0** | 0 ✅ |
| ANN 캐시 빌드(지연, 첫 쿼리) | 2.6 s | — |

채택 C=512(기본값): 최소 통과 C는 256이나 512가 p95 동일(54.7 vs 54.8ms)하면서
recall 0.995로 더 높아 기본값을 유지한다. C=1024는 recall 1.000/p95 60.7ms —
recall 여유가 필요하면 `VECTOR_ANN_COARSE_K=1024`.

**마이그레이션 절차 (기존 vectors.db에 bit 컬럼 backfill):**

```bash
# 1) serve 중지 (테이블 재구성 중 동시 접근 금지)
# 2) 비파괴 backfill — float/문서/메타 불변, 부호비트만 파생(재임베딩 없음)
python scripts/migrate_add_binary_quantization.py \
    --db-path <LOCAL_DATA_DIR>/vectors.db \
    --backup-to <백업경로>.db          # 필수(--skip-backup 명시 시만 생략 가능)
# 3) 활성화 후 재기동
export VECTOR_ANN=binary               # (선택) VECTOR_ANN_COARSE_K=512
opencrab serve
```

- 스크립트는 멱등(`embedding_bit` 이미 있으면 no-op 종료 0)·`--dry-run`·`--batch` 지원.
- vec0는 `ALTER TABLE ADD COLUMN`·안전한 `RENAME`이 불가(가상테이블, RENAME은 shadow
  테이블을 옮기지 않음)하므로 내부적으로 **임시테이블 스테이징 → 원본 재생성 → 재복사**
  패턴을 쓴다. 각 단계 카운트 검증, 실패 시 원본/임시 보존 + 백업 복구 안내.
- 재구성 특성상 파일에 free page 가 남아 크기가 약 2배가 된다 — 마이그레이션 후
  `sqlite3 <db> 'VACUUM;'` 로 회수(선택).

**롤백:** `VECTOR_ANN` 미설정으로 되돌리면 즉시 exact 경로로 복귀한다. 스키마 원복은
불필요 — bit 컬럼은 남아 있어도 미사용일 뿐이며 쓰기 시 계속 동기 유지되므로 나중에
`VECTOR_ANN=binary`를 다시 켜도 마이그레이션 재실행이 필요 없다. bit 컬럼 자체를
제거하려면 마이그레이션 전 백업(`--backup-to`)으로 파일을 되돌린다(스키마 원복).

### `chroma` (docker 모드 기본 / local+minilm 조합 기본)

- **장점**
  - **HNSW ANN**이라 전역(pack 미지정) 검색이 대규모에서도 빠르다.
  - **docker 모드 HTTP 서버 지원**(`CHROMA_HOST`/`CHROMA_PORT`) — 별도 서비스로 분리 운영 가능.
  - **minilm 기본 임베딩 함수 내장** — 별도 서버·GGUF 없이 설정 0으로 바로 동작.
  - 성숙도가 sqlite-vec보다 높음(오래된 프로덕션 사용 이력).
- **단점**
  - `PersistentClient`는 **동일 persist 경로에 대한 다중 프로세스 동시 쓰기를 지원하지
    않는다** — 로컬 모드에서 오프라인 대량 재적재 시 MCP 서버 중지가 필요(`chroma.lock`
    배타 락으로 방어).
  - HNSW는 근사 검색이라 recall이 sqlite-vec의 exact 검색보다 낮을 수 있다
    (실측 recall@10 vs sqlite-vec 0.925).

### `pgvector` (`STORAGE_MODE=pg` 기본 — 구현 완료)

- 스토어 4종(graph/doc/sql/vector)을 PostgreSQL 한 서버로 통합하는 경로.
  **MVCC 다중 라이터**를 제공해 sqlite-vec의 "라이터 직렬화" 제약을 근본적으로 해소한다.
  graph/vector/doc는 factory가 `POSTGRES_URL`당 1회 생성하는 공유 SQLAlchemy 엔진(단일
  커넥션 풀)을 주입받는다.
- **장점**
  - MVCC로 리더가 라이터를 막지 않고, 라이터는 행 단위로만 경합 — 다중 프로세스
    (MCP 서버 + 백그라운드 로더)가 락 없이 동시에 읽고 쓸 수 있다.
  - **HNSW 인덱스**(`vector_cosine_ops`, `m=16, ef_construction=64`, 쿼리 세션
    `hnsw.ef_search`=`PG_EF_SEARCH` 기본 500 — §4.2 Phase 2 게이트 재측정으로 150→500
    상향)가 **전역(pack 미지정) 검색도** 서브선형으로 처리 — 179,784건 전량 실측 global
    p95 **24.61ms**(ef=500). sqlite-vec가 전역 브루트포스(868ms)를 완화하려고 도입한
    binary 2단계 양자화(§4.1) 같은 별도 가속이 애초에 불필요하다.
  - `pg_dump`/`pg_restore`/PITR로 vectors·doc·sql·graph를 한 번에 정합성 있게 백업·복구.
- **단점**
  - 상시 서버 프로세스(RPi5에서 SQLite/Chroma 인프로세스 대비 자원 점유 증가),
    HNSW 빌드 시 CPU/메모리 스파이크(`maintenance_work_mem`/`max_parallel_maintenance_workers`
    튜닝 필요 — 아래 인프라 주의 참고).
  - `EMBEDDING_BACKEND=local`(minilm)과 조합 불가(sqlite-vec와 동일 가드, `ValueError`).
- **pack_id 전용 컬럼(JSONB GIN 미채택)**: `pack_id`를 `metadata` JSONB에 묻지 않고
  전용 컬럼 + btree 인덱스로 분리했다 — 프리플라이트 실증상 JSONB GIN 대비 이점이 없었고,
  `pack_id` 등가/멤버십(`$in`)이 필터의 지배적 패턴이라 btree 등가 조회가 더 단순하고 빠르다.
  나머지 메타 키는 `metadata ->> :key` 조건으로 그대로 SQL WHERE에 완전히 푸시다운된다
  (sqlite-vec의 "LIMIT 이후 Python 후처리 필터" 같은 2단계가 구조적으로 없다).
- **설치**: `pip install ".[pg]"`(pgvector 파이썬 패키지 — Postgres 확장 자체는
  `CREATE EXTENSION vector`를 스토어 ensure-schema가 최초 사용 시 idempotent하게 실행).
- **이관**: 기존 SQLite(graph.db/doc_store.db/opencrab.db/vectors.db) → PG는
  `scripts/migrate_sqlite_to_pg.py`로 1:1 복사(재임베딩 불필요 — sqlite-vec 표준이
  이미 KURE 1024d이므로 벡터는 raw float 그대로 옮긴다).
- 상세 설계·프리플라이트 실측·트레이드오프: [pgvector-migration-plan.md](./pgvector-migration-plan.md) (B) 경로.

#### 4.2 Phase 2 통합 벤치 — sqlite-vec(A) vs pgvector(B) §11.1 게이트 실측

실코드 경로(`PgVectorStore`/`PGGraphStore`/`PgDocStore`, factory가 만드는 것과 동일한
클래스)로 **179,784건 실데이터 KURE 1024d 벡터 전량 + graph 154,561 노드/431,377
엣지 전량 + doc 156,744/64,801/1,711,237(node/source/audit) 전량**을 임시 PG 16
컨테이너(`pgvector/pgvector:pg16`, `--shm-size=1g`)로 1:1 이관(`scripts/migrate_sqlite_to_pg.py
--verify`, 행수 전부 일치 확인)한 뒤 측정했다. 벤치 조건: RPi5, ef_search=150(별도
표기 없는 한 — 측정 당시 `PG_EF_SEARCH` 기본값. recall 게이트 미달로 이후 기본값을
500으로 상향, 아래 재측정 참조).

**이관 소요:** graph+doc+vector 전량 이관 총 약 18분(그중 벡터 bulk-copy 약 4.2분,
`execute_values` batch, 실측 720\~740 rows/s) + HNSW 빌드(`m=16, ef_construction=64`,
`PgVectorStore` 자체 설정대로 `maintenance_work_mem=512MB`/단일스레드) 약 4\~5분.

**벡터 게이트** (`PgVectorStore.query`, 홀더 EF로 저장된 벡터를 그대로 질의에 재사용 —
재임베딩 없음):

| 지표 | 목표 | 실측 | 판정 |
|------|------|------|------|
| global top-10 p50/p95 | (참고) | 8.24 / 22.60 ms | — |
| pack-scoped p95 — 대(10,887건) | ≤100ms | 2.93 / **4.36** ms | ✅ |
| pack-scoped p95 — 중(316건) | ≤100ms | 3.93 / **6.73** ms | ✅ |
| pack-scoped p95 — 소(10건) | ≤100ms | 1.83 / **3.36** ms | ✅ |
| 필터 조합(pack_id+metadata) p95 | ≤200ms | 7.09 / **7.90** ms | ✅ |
| recall@10 (HNSW vs exact, ef_search=500) | ≥0.95 | **0.9600** | ✅ |
| pack isolation leak | 0 | **0** | ✅ |

**recall 게이트 — 수정 및 재측정:** 최초 실측(`ef_search=150`)은 recall@10 0.9440으로
게이트 미달이었다. `PG_EF_SEARCH` 기본값을 500으로 상향(`opencrab/config.py`)하고
179,784건 전량·200쿼리·동일 시드(1234)로 ef별 recall/global p95 곡선을 재실측했다:

| ef_search | recall@10 | global p50/p95 |
|---|---|---|
| 150 | 0.9370 | 5.11 / 10.48 ms |
| 300 | 0.9490 | 7.99 / 15.52 ms |
| 400 | 0.9500 | 10.20 / 21.11 ms(게이트 경계, 마진 부족) |
| **500(채택)** | **0.9600** | **12.00 / 24.61 ms** |
| 550 이상 | 1.0000 | 683.87 / 712.41 ms(지연 급증 — 동시 부하 없는 단독 측정에서도 재현, 하드웨어/캐시 한계로 추정) |

500을 채택값으로 확정했다 — recall 마진(0.96 vs 게이트 0.95)과 지연 마진(24.61ms vs 게이트
100ms, 4배 여유)을 동시에 만족하는 마지막 안전 구간이며, 550 이상에서는 지연이 약
30~60배 급격히 나빠지므로 그 구간은 피해야 한다.

**graph 게이트** (`PGGraphStore.find_neighbors` vs `LocalGraphStore.find_neighbors`,
동일 노드 20개 — 차수 상위 허브 5개(최대 차수 6,586) + 무작위 15개, `direction=both,
depth=3, limit=50`):

| 지표 | 목표 | 실측(수정 후) | 판정 |
|------|------|------|------|
| PGGraphStore 3-hop p50/p95(20노드 전체) | ≤100ms | 5.96 / **11.02** ms | ✅ |
| PGGraphStore 3-hop p95 — 허브 5개만 | ≤100ms | 3.05 / **3.64** ms | ✅ |
| PGGraphStore 3-hop p95 — 무작위 15개만 | ≤100ms | 7.02 / **11.16** ms | ✅ |
| LocalGraphStore 3-hop p50/p95(참고) | — | 4.75 / 9.34 ms | — |

**graph 게이트 — 원인 및 수정 내역:** 최초 실측(102.27/164.82ms)의 원인은
`find_neighbors`가 홉의 각 프론티어 노드마다 엣지를 조회한 뒤 **반환된 각 행마다
목적지/출발지 노드 속성을 개별 SELECT로 재조회**하는 N+1 패턴이었다 — 차수 6,586 허브는
1홉만으로도 최대 ~50회의 개별 SQL 왕복(소켓 비용)이 발생했다. `PGGraphStore.find_neighbors`
(`opencrab/stores/pg_graph_store.py`)를 홉 단위 배치 조회로 재작성해 해소했다: BFS 큐는
FIFO 특성상 항상 레벨(홉) 단위로 진행되므로, 한 홉의 프론티어 노드 전체를
`unnest(:frontier_ids) CROSS JOIN LATERAL (... LIMIT :cap)` 쿼리 1회로 후보 엣지를
모으고(`:cap` = `limit` — 기존 "remaining"이 가질 수 있는 최댓값과 동일한 안전 상한이라
허브의 전체 엣지 목록을 긁어오지 않는 성질은 그대로 유지), 후보 노드 속성도
`unnest`+`JOIN` 쿼리 1회로 모은다. 원본의 "remaining slot" 순차 선택 로직(노드/방향/행
순서, pack 필터 3규칙)은 메모리상에서 그대로 재현했다 — SQL은 후보 수집만 배치화했을 뿐
선택 로직은 손대지 않아 파리티가 보존된다. 파리티 검증: `tests/test_pg_graph_doc_parity.py`
36개 전부 통과(`OPENCRAB_PG_TEST_URL` 설정 시). 재귀 CTE(§6.4 canonical 경로)로의 전환은
여전히 미착수 상태이나, 이번 홉 단위 배치화만으로 게이트를 9배 이상 여유 있게 통과했다.

**doc 스팟체크** (`PgDocStore.keyword_search`, 실데이터 질의 3종):

| 질의 유형 | 질의 | 지연 | 결과 |
|---|---|---|---|
| 영어 | `clinical trial biomarker` | 44.81ms | 0건(코퍼스에 매치 없음 — sanity, 게이트 아님) |
| 약어 | `MRR` | 7.07ms | 2건, 내용 타당 |
| 모델번호 | `KURE-v1` | **2873.17ms** | 10건, 내용 타당하나 지연 큼 — 짧은 토큰(`v1` 등) trigram ILIKE 폴백 경로가 인덱스를 충분히 활용하지 못한 것으로 추정(§ pg_doc_store.py KEYWORD SEARCH 주석 참고, 후속 조사 필요) |

**pack delete 정합성** (`PgVectorStore.delete`, 소형 팩 `shiftone-dutch-coffee-assets`
4건 삭제):

| 지표 | 목표 | 실측 | 판정 |
|---|---|---|---|
| 잔존 행(해당 pack_id) | 0 | 0 | ✅ |
| 고아 행(삭제된 id) | 0 | 0 | ✅ |
| 전체 행수 델타 | -4 | -4(179,784→179,780) | ✅ |

**백업/복구 게이트** (`pg_dump -Fc` → DB drop/recreate → `pg_restore`, 4스토어 행수
재대조):

| 항목 | 실측 |
|---|---|
| `pg_dump` 소요 / 덤프 크기 | 2m22.6s / 730MB |
| `pg_restore` 소요(HNSW 재빌드 포함, 병렬 3워커 자동 채택) | 6m41.0s |
| 복구 후 행수 | graph_nodes 154,561 / graph_edges 431,377 / doc_nodes 156,744 / doc_sources 64,801 / audit_log 1,711,237 / opencrab_vectors_kure 179,780 — **사전 수치와 전부 일치** |
| 게이트(1커맨드 왕복 완전 복구) | ✅ **PASS** |

**라이브 무간섭** (적재/HNSW 빌드/restore 전 구간, `POST /mcp initialize` 프로브):

| 시점 | 응답시간 | free 가용량 |
|---|---|---|
| 적재 전(베이스라인) | 8.1ms | 5.3Gi |
| 벡터 적재 중 | 1.2ms | 5.2Gi |
| HNSW 빌드 직후 | 2.6ms | 5.1Gi |
| 전 과정 종료 후 | 7.3ms | 5.4Gi |

전 구간 서브10ms 유지, 라이브 서비스에 관측 가능한 저하 없음(swap 1.4→1.7Gi 소폭
증가했으나 available 헤드룸 충분).

**cold start / disk usage** (SQLite 3파일 대비 PG 3스토어, 동일 실데이터):

| 항목 | SQLite(3파일 합계) | PG(3테이블군 합계) | 비고 |
|---|---|---|---|
| 디스크 사용량 | 2,474 MB(graph 299 + doc 970 + vector 1,205) | **3,584 MB**(graph 260 + doc 806 + vector 2,518) | **PG가 약 45% 큼** — HNSW 인덱스(벡터 테이블 2,518MB 중 상당 비중) + JSONB/TOAST + PG 튜플 오버헤드가 원인. VACUUM 미실행 상태 수치 |
| 콜드 커넥션(최초 1회) | 0.4ms(파일 open) | 73.1ms(TCP 커넥션+ping) | 상시 서버 프로세스 특성상 1회성 비용, 커넥션 풀 재사용 후 steady-state 영향 없음 |

**§11.1 게이트 종합 판정** (pgvector, 179,784×1024d 전량, ef_search=500 · find_neighbors
홉 단위 배치화 적용 기준 — 수정 후 재측정):

| 게이트 | 목표 | 실측 | 판정 |
|---|---|---|---|
| single-pack top-k p95 | ≤100ms | 2.93\~6.73ms(대/중/소) | ✅ |
| metadata-filtered top-k p95 | ≤200ms | 7.90ms | ✅ |
| 3-hop graph traversal p95 | ≤100ms | **11.02ms**(수정 전 164.82ms) | ✅ |
| recall@10 (HNSW vs exact) | ≥0.95 | **0.9600**(수정 전 ef=150 기준 0.9440) | ✅ |
| pack isolation leakage | 0 | 0 | ✅ |
| pack delete consistency | orphan 0 | 0 | ✅ |
| backup/restore | 1 command 완전 복구 | 행수 100% 일치 | ✅ |
| cold start / disk usage | 측정·기록 | 디스크 +45%, 콜드스타트 +73ms | 기록(악화 명시) |

**결론:** 최초 게이트 실측은 8개 중 6개 PASS, 2개(3-hop graph p95, recall@10) FAIL이었다.
두 FAIL 모두 원인을 특정해 해소했다 — recall은 `PG_EF_SEARCH` 기본값을 500으로 상향(ef별
곡선 실측으로 550 이상의 지연 급증 구간을 확인하고 그 직전 안전값을 채택), graph는
`find_neighbors`를 홉 단위 배치 조회(unnest+LATERAL)로 재작성해 N+1 SQL 왕복을 제거했다
(재귀 CTE 전환은 여전히 미착수 상태로 남아 있으나 이번 배치화만으로 게이트를 만족).
재측정 결과 8개 게이트 전부 PASS. sqlite-vec(A) 쪽 §11.1 실측(pack-scoped p95 8.3ms
exact / global p95 exact 593ms·binary 55ms recall 0.995)과 나란히 보면, pack-scoped
지연은 pgvector가 근소 우위(2.93\~6.73ms vs 8.3ms)이나 절대 격차는 작고, global 검색은
pgvector(HNSW, ef=500 기준 24.61ms)가 sqlite-vec의 binary 2단계(54.8ms)보다 빠르면서
recall도 게이트를 만족한다(0.96). graph 3-hop 지연도 수정 후 11.02ms로 (A)의 인프로세스
SQLite(LocalGraphStore, 4.75\~9.34ms)에 근접한다. graph/doc/backup 축은 pgvector 전용
이점(단일 트랜잭션 백업, MVCC 다중 라이터)이 뚜렷하며, 두 FAIL이 해소됨에 따라 (B) 채택의
성능 측 장애 요인은 남아 있지 않다. 최종 채택은 §9 힌지와 함께 확정한다.

---

## 5. 임베딩 축 (`EMBEDDING_BACKEND`)

임베딩 백엔드는 벡터 스토어 백엔드와 독립이지만, 위 §2 규칙 때문에 로컬 모드의
벡터 스토어 기본값에 간접적으로 영향을 준다.

- **`openai` (기본)**: OpenAI 호환 `/v1/embeddings` 서버(LM Studio·Ollama·vLLM·실제
  OpenAI 등)를 primary로, 로컬 GGUF를 fallback으로 쓰는 `ResilientEmbeddingFunction`
  구조. primary 서버가 죽으면 15초 TTL로 GGUF 폴백에 자동 전환하고 복구 후 자동 복귀한다.
  GGUF 폴백은 KURE-v1-Q8_0(약 635MB, 품질 우선 — Q4_K_M은 `LOCAL_GGUF_PATH`로 지정
  가능한 저사양 대안)을 최초 실행 시 HuggingFace에서 자동 다운로드하며,
  `pip install "opencrab[gguf]"`로 `llama-cpp-python`을 설치해야 동작한다. 즉 외부 서버
  없이도 완전 로컬로 동작 가능하다. 한국어 검색 품질 실측: KURE-v1 MRR 1.000 (top-1 5/5)
  vs minilm MRR 0.285 (top-1 0/5).
- **`local` (롤백 옵션)**: ChromaDB 내장 EF(all-MiniLM-L6-v2 ONNX, 384d). 설정 없이 바로
  동작하지만 영어 특화 모델이라 한국어 검색 품질이 낮다. `VECTOR_BACKEND=sqlite-vec`와
  조합 불가(§2 참고).

---

## 6. 마이그레이션 / 롤백 절차

### Chroma → sqlite-vec 전환

```bash
python scripts/migrate_chroma_to_sqlite_vec.py   # 기존 chroma 컬렉션 → vectors.db, 원본 1:1 이관(재임베딩 없음)
export EMBEDDING_BACKEND=openai VECTOR_BACKEND=sqlite-vec
opencrab serve
```

- 스크립트는 **재임베딩하지 않는다** — KURE 벡터를 그대로 복사한다. `--dry-run`,
  `--force`, `--batch` 옵션 지원. Chroma 원본은 삭제하지 않는(비파괴) 동작.

### SQLite(local/kuzu) → PG-unified (`STORAGE_MODE=pg`) 전환

```bash
pip install ".[pg]"
python scripts/migrate_sqlite_to_pg.py --pg-url "$POSTGRES_URL" --dry-run   # 계획 확인
python scripts/migrate_sqlite_to_pg.py --pg-url "$POSTGRES_URL" --verify   # 4스토어 1:1 이관 + 행수 검증
export STORAGE_MODE=pg POSTGRES_URL=postgresql://...
opencrab serve
```

- **재임베딩 불필요** — sqlite-vec 표준이 이미 KURE(1024d)이므로 `vectors.db`의 raw
  float 벡터를 `vec_to_json`으로 읽어 그대로 pgvector에 복사한다(§3.2/§4.3 정정 —
  아래 pgvector-migration-plan.md §3.2 참고).
- 원본 SQLite 파일은 **읽기 전용으로만 접근**(마이그레이션 스크립트가 절대 쓰지 않음) —
  `--backup-to` 없이도 원본 무변경이 보장된다. `--verify`로 이관 후 행수 대조.
- 멱등 — 이미 이관된 테이블(행수 일치)은 재실행 시 스킵.

### sqlite-vec → Chroma 롤백

```bash
export VECTOR_BACKEND=chroma
opencrab serve
```

- 기존 Chroma 컬렉션이 그대로 남아 있으므로 설정만 되돌리면 즉시 복귀한다(비파괴).

### openai → local(minilm) 임베딩 롤백

```bash
export EMBEDDING_BACKEND=local
opencrab serve
```

- `VECTOR_BACKEND=sqlite-vec`가 명시돼 있으면 기동 실패(§2)하므로, minilm으로
  롤백할 때는 `VECTOR_BACKEND=chroma`도 함께 지정해야 한다.

---

## 7. 관련 환경변수

| 환경변수 | 기본값 | 설명 |
|----------|--------|------|
| `VECTOR_BACKEND` | _(미설정 — §2 조건부 규칙)_ | `chroma` \| `sqlite-vec` \| `pgvector` |
| `VECTOR_DB_FILE` | `vectors.db` | sqlite-vec 벡터 DB 파일명(`LOCAL_DATA_DIR` 하위) |
| `VECTOR_COLLECTION` | `vectors_kure` | sqlite-vec vec0 테이블명 |
| `VECTOR_ANN` | _(미설정 = off)_ | `binary` = 전역 검색 2단계 양자화 가속(§4.1, sqlite-vec 전용) |
| `VECTOR_ANN_COARSE_K` | `512` | binary 2단계 coarse 후보 수 C(recall 튜닝 노브, ≤4096) |
| `PG_EF_SEARCH` | `500` | pgvector HNSW 쿼리 세션 파라미터 `hnsw.ef_search`(recall/속도 트레이드오프, pgvector 전용) — §4.2 재측정으로 150→500 상향(recall@10 게이트 ≥0.95 확보) |
| `EMBEDDING_BACKEND` | `openai` | `openai` = OpenAI 호환 서버+GGUF 폴백, `local` = minilm |
| `OPENAI_API_BASE` | `http://<server-host>:1234/v1` | OpenAI 호환 서버 주소. 콤마로 여러 URL 을 나열하면 순서대로 시도하는 체인이 된다(예: `http://a:1234/v1,http://b:1234/v1`) — 첫 서버 장애 시 다음 서버, 전부 장애 시 GGUF 폴백. 단일 URL 이면 기존과 동일 |
| `OPENAI_EMBED_MODEL` | `text-embedding-kure-v1` | 서버에 로드된 임베딩 모델 id |
| `EMBED_DIM` | `1024` | 임베딩 차원(모델에 맞게 설정) |
| `LOCAL_GGUF_PATH` | _(자동 다운로드)_ | 로컬 GGUF 폴백 경로 |
| `EMBED_COLLECTION` | `opencrab_vectors_kure` | openai 백엔드 전용 Chroma 컬렉션명(`VECTOR_BACKEND=chroma`일 때) |
| `CHROMA_HOST` / `CHROMA_PORT` | `localhost` / `8000` | docker 모드 Chroma HTTP 서버 주소 |
| `CHROMA_COLLECTION` | `opencrab_vectors` | minilm(local) 임베딩 전용 Chroma 컬렉션명 |

---

## 8. 팩 단위 raw 벡터 export/import 계약 (#200)

세 벡터 스토어(`chroma` / `sqlite-vec` / `pgvector`)는 **재임베딩 없이 한 팩의 벡터를 통째로
복제**하는 두 메서드를 공개한다. 소비자는 `pack_fork`(#201)다.

```python
store.export_pack_vectors(pack_id) -> list[record]
store.import_vectors(records, *, pack_id) -> list[str]

record = {"id": str, "embedding": list[float],
          "document": str | None, "metadata": dict,
          "uris": str | None}      # chroma 전용
```

계약 본문(왜 그렇게 정했는지)은 `opencrab/stores/_vector_base.py` 모듈 docstring 의
"pack-scoped raw vector export/import" 절이 정본이다. 여기에는 **백엔드별로 다른 것**만 적는다.

### 8.1 왕복 충실도 — 백엔드마다 다르다

| 백엔드 | 임베딩 왕복 | 확인 방법 |
|---|---|---|
| `sqlite-vec` (float-only, `ann=binary` 공히) | **바이트 동일** | 저장된 blob 을 직접 대조 |
| `pgvector` | **정확히 동일** | pgvector `=` 연산자 |
| `chroma` | **동일하지 않다 — 성분당 최대 1 float32 ULP** | 성분별 ULP 허용오차 + KNN 순서 동일 |

**chroma 가 어긋나는 이유는 `hnsw:space` 다.** `ChromaStore` 는 언제나 `cosine` 으로 컬렉션을
만드는데, 그 공간에서는 정확히 float32 인 입력조차 되읽으면 일부 성분이 1 ULP 이동한다
(`l2`/`ip` 는 바이트 동일). 결정적이고 재오픈에도 안정하며 KNN 순서는 보존되지만, **멱등은
아니다** — 이미 저장된 값을 다시 넣어도 또 이동할 수 있다. 그래서 이 축을 "해시 동일"로
검증하면 안 된다. 세 백엔드에 같은 해시 게이트를 걸 수 없다는 것이 이 표의 요지다.

`sqlite-vec`/`pgvector` 축이 "backend raw 표현(저장된 blob·컬럼) 동일"인 반면 `chroma` 축은
그 수준을 달성할 수 없어 **canonical float32 허용오차**로 판정한다.

재현: `tests/test_vector_raw_contract.py::TestRoundTrip::test_embedding_survives_the_round_trip`
(`sqlite-vec` 축은 확장 모듈이 없으면 skip 된다 — 아래 8.5).

### 8.2 정체성 제약 — 슬롯 키는 `node_id` 단독, 소유는 `pack_id` 가 지킨다 (#197)

슬롯 키는 `node_id` 하나이고 **팩으로 한정되지 않는다.** 그래서 서로 다른 팩이 같은 `node_id` 를
낼 수 있다. #197 이전에는 마지막에 쓴 팩이 그 슬롯을 통째로 가져갔고, 먼저 쓴 팩의 문서와
임베딩이 조용히 사라졌으며 그 팩으로 스코프한 질의가 0건이 됐다. 세 백엔드 전부 같은 결과였다.

**`upsert_texts` 는 이제 그런 쓰기를 거부한다.** 계약은 한 문장이다: **소유된 슬롯은 그 소유자만
다시 쓴다.**

| 기존 행의 `pack_id` | 들어오는 meta 의 `pack_id` | 판정 |
|---|---|---|
| 행 없음 | 무엇이든 | 통과(신규 슬롯) |
| 비어 있음(`""`·부재·`None`) | 무엇이든 | 통과(미소유 슬롯 인수 — 백필·마이그레이션이 쓰는 경로) |
| 비어 있지 않음, 같음 | 같음 | 통과(자기 팩 재적재) |
| 비어 있지 않음 | 다름(빈 값 포함) | **거부(`ValueError`)** |

한 배치가 같은 id 로 **서로 다른 소유 상태**를 주장해도 거부한다. 미소유도 하나의 상태로 참여하므로
팩 하나와 소유 없음이 한 id 를 다투는 것도 거부다. 빈 슬롯에서는 두 레코드가 모두 "행 없음" 으로
읽히므로, 쓰기 순서가 소유자를 정하게 두지 않는다. 미소유끼리의 중복은 같은 값이라 충돌이 아니다.
배치는 적용 전에 통째로 판정되므로 거부된 배치는 부분 적용을 남기지 않는다.

소유 태그를 **저장하는** 자리도 읽는 자리와 같은 정규화를 거친다. `pack_id` 가 `None` 인 메타를
`str(meta.get("pack_id", ""))` 로 쓰면 리터럴 `"None"` 이 저장되는데, 선검사는 그 메타를 미소유로
읽으므로 같은 메타의 재적재가 거부된다. 쓰기와 읽기가 같은 함수를 거쳐 그 어긋남이 생기지 않는다.

**강제는 두 층이다.** 선검사가 배치를 판정하고 호출자가 보는 오류를 만든다. 그 위에 SQL 두
백엔드는 쓰기문 자신에 소유권 술어를 둔다. vec0 은 자기 행이나 미소유 행만 지우고(남의 행이
살아남아 뒤이은 INSERT 가 기본키 충돌로 실패한다), pgvector 는 `DO UPDATE` 에 저장된 `pack_id`
술어를 걸고 `rowcount == 0` 을 위반으로 읽는다.

두 술어 모두 `pack_id IS NULL` 을 먼저 본다. SQL 에서 `NULL = ''` 은 거짓이 아니라 NULL 이라,
그것을 안 보면 저장된 값이 NULL 인 행에서 술어가 NULL 로 평가돼 선검사와 판정이 갈린다.
두 백엔드 모두 이 컬럼이 NOT NULL 이 아니어서 외부 기록이 그 값을 남길 수 있다.

층 2 는 선검사의 잠금 없는 SELECT 와 쓰기 사이에 다른 프로세스가 소유자를 바꾸는 창을 닫으며,
둘 다 트랜잭션 안이라 배치가 롤백된다. chroma 는
조건부 쓰기도 트랜잭션도 없고 프로세스 간 잠금이 공유 락이며 MCP 전용이라(#140) 선검사와
프로세스 내 락까지가 한계다. 프로세스 간 직렬화는 종전에도 이 계층이 제공하지 않았고 지금도
호출자의 `write.lock` 규율이다.

**계약 밖**: 쓰기만 규율한다. `delete(ids)` 는 여전히 팩을 가리지 않고 그 id 의 행을 지운다.
`add_texts` 도 게이트를 걸지 않는다(시간 소금 id 이고 SQL 두 백엔드가 중복 기본키를 이미 거부한다).
게이트 도입 이전에 이미 넘어간 슬롯은 치유하지 않는다.

한 배치 안에서 같은 id 가 **같은 소유 상태**를 두 번 주장하는 것은 이 이슈와 무관해 백엔드별 현행
동작을 그대로 둔다. 같은 팩을 두 번 대는 것도, 소유 없음을 두 번 대는 것도 여기 든다.

| 백엔드 | 같은 상태 중복 id 한 배치 | 다른 팩에 같은 `node_id` 를 add |
|---|---|---|
| `sqlite-vec` | 통과, 마지막 값이 남는다 | 거부(`UNIQUE constraint failed`) |
| `pgvector` | 통과, 마지막 값이 남는다 | 거부(`UniqueViolation`) |
| `chroma` | 거부(`DuplicateIDError`) | **거부하지 않는다. 조용히 무시된다** — 기존 레코드가 이긴다 |

`add` 축에서 chroma 만 fail-open 이라 `ChromaStore.import_vectors` 가 **스토어 안에서** 중복을
선검사해 거부한다. 그래서 "이미 있는 id 는 예외"는 세 백엔드에서 모두 성립한다. **예외 타입은
통일하지 않는다**(각 백엔드의 것이 그대로 올라온다) — 호출자는 타입으로 분기하지 말고 "예외 =
이 배치 실패"로 다루고 보상한다. 쓰기 게이트가 내는 `ValueError` 는 이 계층 자신의 판정이라
예외지만, 호출자 규칙은 같다.

`import_vectors` 는 쓰기 게이트보다 **엄격하다**. 게이트는 팩이 자기 슬롯을 다시 쓰는 것을
허용하지만, import 는 이미 있는 id 를 소유자와 무관하게 거부한다. **따라서 fork 는 id 재매핑이
필수다.**

재현: `pytest tests/test_vector_slot_ownership.py`,
`pytest tests/test_store_concurrency.py -k SlotOwnership`,
`pytest tests/test_pack_load.py -k SlotOwnershipThroughTheRealStores`

### 8.3 호출자가 재작성해야 하는 것

계약이 스탬프하는 metadata 키는 **`pack_id` 하나뿐**이다(없거나 같으면 대입, 다르면 거부).
폐기 별칭 `pack` 은 버린다(#159/#171 — 남기면 새 팩에 남의 팩명이 다시 심긴다).

원본 팩의 id-space 를 가리키는 나머지 키는 **호출자가 재작성한다**:

| 키 | 재작성 안 하면 |
|---|---|
| `node_id` | 벡터 히트의 노드 정체성을 이 키로 먼저 해석하므로, 사본 팩 히트가 **원본 팩 노드 id** 를 가리킨다 |
| `source_id` / `document_id` | 소스·문서 참조가 원본을 가리킨다 |
| `source` | 사본이 **원본 팩명**을 달고 산다(벡터 축 소유 판정은 `pack_id` 단독이라 오늘은 무해하나 doc 축은 이 키를 폴백으로 본다) |

계약이 `node_id` 등식(`metadata["node_id"] == id`)을 **강제하지 않는 것은 의도**다. 그 등식은
노드 벡터에서만 성립하고, 청크 벡터는 이 키로 **소유 노드를 가리키는 것이 정상**이라 강제하면
정상 팩의 fork 가 막힌다.

**추가 불변식**: 벡터 id 는 그 팩의 노드·청크 id 와 같아야 한다. `pack/load.py` 의 증분 적재가
`live_vec_ids - (노드 id | 청크 id)` 를 고아로 보고 **실제로 삭제**하므로, 벡터만 재매핑하고
graph/doc 쪽을 그대로 두면 다음 적재에서 임포트한 벡터가 전량 사라진다.

### 8.4 chroma 의 한계

- **원자적이지 않다.** 트랜잭션이 없고, 클라이언트의 `get_max_batch_size()` 를 넘는 배치는
  스토어가 나눠 넣으므로 중간 실패 시 앞 청크가 남는다(상한값은 클라이언트·버전마다 다르다 —
  `python3 -c "import chromadb,tempfile;print(chromadb.PersistentClient(path=tempfile.mkdtemp()).get_max_batch_size())"`
  로 지금 값을 확인한다). 보상은 호출자 몫이다
  (`sqlite-vec` 은 `_tx()`, `pgvector` 는 `engine.begin()` 단일 트랜잭션이라 전량 롤백된다).
- **선검사와 실제 쓰기 사이 창이 남는다.** 프로세스 안의 `ChromaStore` 공개 쓰기끼리만
  직렬화되고, 프로세스 간은 호출자의 `write.lock` 규율이다. 그 창을 완전히 닫지는 못하므로
  import 는 **쓰기 후 되읽어** 전 id 의 존재와 metadata·document·uri 일치를 확인한다 —
  조용한 누락을 예외로 승격시키는 장치다. 임베딩은 대조하지 않는다(비용 + 8.1 의 ULP).
- **import 하는 동안 그 컬렉션의 읽기가 밀린다.** 선검사부터 사후 확인까지 스토어 락을 쥐므로
  같은 컬렉션의 `query`·`count`·`get_by_id`(miss 재확인)가 그 시간만큼 대기한다. 대형 배치에서는
  초 단위다. `upsert_texts` 가 세운 기존 패턴의 연장이고 정확성 문제는 아니지만, fork 를 서빙
  중에 돌리면 검색 지연으로 보인다.
- **metadata 값은 그대로 보존되지 않는다.** `_sanitize_metadata` 가 비스칼라를 `str()` 로
  바꾸고, NaN/Inf 값은 키째 사라지며, 매우 큰 int 는 float 로 강등된다. 전부 `add_texts` 의
  기존 관례와 같고 export→import 경로에서는 도달하지 않는다(export 값은 이미 chroma 가 저장한
  것이다). 반면 `pgvector` 는 중첩 metadata 를 jsonb 로 그대로 보존하므로, 계약은 **metadata
  값 타입을 좁히지 않는다**(좁히면 그런 pg 팩의 fork 가 막힌다).

### 8.5 검증 실행

```
# 3백엔드 계약 전량 (sqlite-vec 확장이 없으면 그 축은 skip)
PYTHONPATH=. python3 -m pytest -q tests/test_vector_raw_contract.py

# sqlite-vec 축까지 태우려면 확장을 격리 설치해 PYTHONPATH 에 얹는다
#   pip install --target ./vecpkg sqlite-vec      (공용 환경에 설치하지 말 것)
PYTHONPATH=.:./vecpkg python3 -m pytest -q tests/test_vector_raw_contract.py

# pgvector 축은 *_test DB 를 가리키는 DSN 이 있어야 돈다(없으면 skip)
OPENCRAB_PG_TEST_URL=postgresql://.../opencrab_test PYTHONPATH=. \
  python3 -m pytest -q tests/test_vector_raw_contract.py
```

세 축 모두 **skip 이면 아무것도 증명하지 않는다** — 백엔드를 실제로 태운 결과인지 `-rs` 로
확인할 것.

### 8.6 fork 소비자 (`opencrab/pack/fork.py`, #201)

이 계약의 실제(유일한) 소비자는 `pack_fork` 다. 오케스트레이터가 이 계약 위에 얹는 것 네 가지만
여기 적는다 — 나머지 정책(2단 오류 모델, 완전성 하한 등)은 `fork.py` 자체와 이슈 #201 설계
문서가 정본이다.

- **원본 팩 자신의 앵커 벡터는 절대 복사하지 않는다** (8.3 표의 `node_id` 재작성 규칙과
  별개로, `id == anchor_node_id(src_pack_id)` 인 record 자체를 배치에서 제외한다). 새 팩은
  자기 앵커 쓰기 경로가 자기 벡터를 새로 만들므로, 원본 앵커 벡터를 재매핑해 넣으면 그 경로가
  이미 점유한 id 에 `import_vectors` 가 부딪혀 8.2 의 "이미 있는 id 는 예외" 규칙대로 배치
  전체가 실패한다 — `pack_create` 로 만든 보통의 팩은 전부 자기 앵커 벡터를 갖고 있으므로, 이
  제외가 없으면 **모든** 평범한 팩의 fork 가 깨진다.
- **배치 실패는 예외가 아니라 등록부 상태 강등으로 처리한다.** 8.4 가 말하는 "보상은 호출자
  몫"은 여기서는 **보상 삭제**를 뜻하지 않는다 — `pack_fork` 는 부분 실패를 이미 쓴 것을
  되돌리는 방식이 아니라, 등록부 행을 `partial` 로 강등하는 방식으로 흡수한다(#170 의 흡수
  상태 모델과 정합; 근거는 `fork.py`의 §6-1 주석과 이슈 #201 설계 §6-1). `ChromaStore.import_vectors`
  독스트링의 "Compensation belongs to the caller (pack_fork)"라는 문구를 문자 그대로 "fork 가
  삭제로 보상한다"로 읽지 말 것 — 실제 보상은 상태 강등이다.
- **한 배치 안에서 같은 `id` 는 슬롯을 하나만 갖고, 그 슬롯은 검사를 통과하는 첫 레코드가
  가진다 (계약 V-KF, #221).** `pack_fork` 는 `import_vectors` 를 부르기 전에 export 배치를
  2-pass 로 분해한다. pass 1 은 8.2 의 id 유일성과 8.1 의 차원 균일성을 배치 안에서 미리
  강제하는데, 그때 **버려진 레코드는 자기 id 를 소진하지 않는다.** 같은 id 의 뒤 레코드가
  여전히 슬롯을 가질 수 있다는 뜻이다. 배치 참조 차원은 pass 1 입력의 **첫 레코드**가 정한다
  (다수결도 아니고 스토어의 `_dim` 도 아니다). 앵커 레코드는 pass 1 이전에 제외되므로 참조
  차원을 세우지 않는다. 유의할 비대칭이 하나 있다. `validate_import_records` 자신은 중복 검사
  직후 무조건 id 를 소진하는 id 우선 규칙이고, pass 1 이 그와 다른 것은 의도다. "검증기와
  통일"하려고 pass 1 을 고치면 유효 레코드를 가진 id 가 버려져 완전성 하한을 밟는다. 계약
  조항과 각 조항을 고정하는 테스트는 `tests/test_pack_fork.py` 의
  `TestVectorBatchDecomposition` 에 있다.
- **위 두 배치 검사가 실제로 발화하는 조건.** 스토어가 스스로 선언한 정규 스키마가 유지되는
  인스턴스에서는 세 백엔드의 `export_pack_vectors` 로 중복 id 나 차원 혼재가 나오지 않는다.
  chroma 는 컬렉션이 id 유일성과 차원을 강제하고, sqlite-vec 은 vec0 선언이, pgvector 는 PK 와
  `vector(n)` 이 강제하기 때문이다. 도달 경로는 세 가지다. 두 메서드만 요구하는 duck-typed
  스토어, 테스트의 export monkeypatch, 그리고 **기존 객체의 스키마를 시동 시 대사하지 않아
  생긴 비정규 스키마**(pgvector 의 기존 테이블, sqlite-vec 의 동명 객체 선점)다. 마지막 축은
  이슈 #232 가 소유한다. 또한 세 백엔드 어느 것도 export 순서를 약속하지 않는다(어느 export
  질의에도 `ORDER BY` 가 없다). 따라서 "같은 팩을 두 번 fork 하면 같은 벡터가 복사된다"는
  성질을 이 계약은 약속하지 않는다. 지금 상태를 직접 확인하려면
  `grep -n "ORDER BY" opencrab/stores/*_store.py` 와 각 스토어의 스키마 생성 함수를 보라.
