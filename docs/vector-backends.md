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
| `STORAGE_MODE` | `local`(기본) / `kuzu` / `docker` / `pg` | 그래프·문서·SQL 스토어 위치. `local`/`kuzu`는 `is_local=True`(둘 다 SQLite/임베디드), `docker`는 Neo4j/MongoDB/PostgreSQL 외부 서비스, `pg`는 4스토어 전부 PostgreSQL(별도 분기, `is_local=False`) |
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
| `local`/`kuzu` | `openai`(기본) | _(미설정)_ | **`sqlite-vec`** | 아무 설정 없이 로컬 실행 시 기본 경로 |
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
    `hnsw.ef_search`=`PG_EF_SEARCH` 기본 150)가 **전역(pack 미지정) 검색도** 서브선형으로
    처리 — 실측 p95 **6.44ms**. sqlite-vec가 전역 브루트포스(868ms)를 완화하려고 도입한
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
--verify`, 행수 전부 일치 확인)한 뒤 측정했다. 벤치 조건: RPi5, ef_search=`PG_EF_SEARCH`
기본값 150(별도 표기 없는 한).

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
| recall@10 (HNSW vs exact, ef_search=150) | ≥0.95 | **0.9440** | ❌ |
| pack isolation leak | 0 | **0** | ✅ |

**recall 게이트 FAIL 원인:** `PG_EF_SEARCH` 기본값 150은 프리플라이트(소규모)에서 정한
값으로, 179,784건 전량에서는 마진이 부족하다. ef_search 스윕(100쿼리, 동일 시드) 실측 —
`ef=150`: 0.9310, `ef=300`: 0.9430, `ef=500`: **0.9550**(게이트 통과). global p95가
8.24/22.60ms로 게이트(≤100ms) 대비 여유가 크므로, `ef_search`를 500 부근으로 올려도
지연 게이트는 유지될 가능성이 높다 — **채택 시 `PG_EF_SEARCH` 기본값 상향 필요**(후속 조치,
본 벤치 범위 밖).

**graph 게이트** (`PGGraphStore.find_neighbors` vs `LocalGraphStore.find_neighbors`,
동일 노드 20개 — 차수 상위 허브 5개(최대 차수 6,583) + 무작위 15개, `direction=both,
depth=3, limit=50`):

| 지표 | 목표 | 실측 | 판정 |
|------|------|------|------|
| PGGraphStore 3-hop p50/p95 | ≤100ms | 102.27 / **164.82** ms | ❌ |
| LocalGraphStore 3-hop p50/p95(참고) | — | 4.75 / 9.34 ms | — |

**graph 게이트 FAIL 원인:** `PGGraphStore.find_neighbors`는 `LocalGraphStore`와 동일하게
Python 오케스트레이션 BFS(단일 재귀 CTE 아님, §6.4 설계대로)라, 홉마다 노드 수만큼 개별
SQL 왕복이 발생한다. `LocalGraphStore`는 인프로세스 SQLite라 왕복 비용이 사실상 0인 반면,
`PGGraphStore`는 매 왕복이 소켓(로컬호스트 TCP)을 타 — 고차수 허브(차수 6,583)에서
왕복 횟수가 누적돼 게이트를 최대 65% 초과했다. 재귀 CTE로 왕복 1회에 묶는 대안은
§6.4에서 이미 canonical 경로로 채택돼 있으나 `find_neighbors`는 아직 Python BFS 포트
상태 — **재귀 CTE 전환이 후속 조치**(본 벤치는 현 구현 그대로 측정, 조작 없음).

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

**§11.1 게이트 종합 판정** (pgvector, 179,784×1024d 전량, ef_search=150 기준):

| 게이트 | 목표 | 실측 | 판정 |
|---|---|---|---|
| single-pack top-k p95 | ≤100ms | 2.93\~6.73ms(대/중/소) | ✅ |
| metadata-filtered top-k p95 | ≤200ms | 7.90ms | ✅ |
| 3-hop graph traversal p95 | ≤100ms | 164.82ms | ❌ |
| recall@10 (HNSW vs exact) | ≥0.95 | 0.9440 | ❌ |
| pack isolation leakage | 0 | 0 | ✅ |
| pack delete consistency | orphan 0 | 0 | ✅ |
| backup/restore | 1 command 완전 복구 | 행수 100% 일치 | ✅ |
| cold start / disk usage | 측정·기록 | 디스크 +45%, 콜드스타트 +73ms | 기록(악화 명시) |

**결론:** 8개 게이트 중 6개 PASS, 2개(3-hop graph p95, recall@10) FAIL. 두 FAIL 모두
원인이 특정돼 있고 **설정/구현 조정으로 해소 가능**한 성격이다 — recall은 `PG_EF_SEARCH`
상향(500 부근, 지연 여유 충분), graph는 `find_neighbors`의 재귀 CTE 전환(§6.4 canonical
경로로 이미 설계됨, 미구현 상태)이 후속 조치다. sqlite-vec(A) 쪽 §11.1 실측(pack-scoped
p95 8.3ms exact / global p95 exact 593ms·binary 55ms recall 0.995)과 나란히 보면,
pack-scoped 지연은 pgvector가 근소 우위(2.93\~6.73ms vs 8.3ms)이나 절대 격차는 작고,
global 검색은 pgvector(HNSW, ef=150 기준 22.60ms)가 sqlite-vec의 binary 2단계(54.8ms)보다
빠르면서 recall 조정 여지도 있다(ef_search 상향). graph/doc/backup 축은 pgvector 전용
이점(단일 트랜잭션 백업, MVCC 다중 라이터)이 뚜렷하나 graph 3-hop 지연은 현 구현 상태로는
(A)의 인프로세스 SQLite(LocalGraphStore, 4.75\~9.34ms)에 크게 못 미친다. (A)/(B) 최종
채택은 이 두 FAIL의 해소 여부(§9 힌지와 함께)를 확인한 뒤 재게이트로 확정한다.

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
| `PG_EF_SEARCH` | `150` | pgvector HNSW 쿼리 세션 파라미터 `hnsw.ef_search`(recall/속도 트레이드오프, pgvector 전용) |
| `EMBEDDING_BACKEND` | `openai` | `openai` = OpenAI 호환 서버+GGUF 폴백, `local` = minilm |
| `OPENAI_API_BASE` | `http://<server-host>:1234/v1` | OpenAI 호환 서버 주소. 콤마로 여러 URL 을 나열하면 순서대로 시도하는 체인이 된다(예: `http://a:1234/v1,http://b:1234/v1`) — 첫 서버 장애 시 다음 서버, 전부 장애 시 GGUF 폴백. 단일 URL 이면 기존과 동일 |
| `OPENAI_EMBED_MODEL` | `text-embedding-kure-v1` | 서버에 로드된 임베딩 모델 id |
| `EMBED_DIM` | `1024` | 임베딩 차원(모델에 맞게 설정) |
| `LOCAL_GGUF_PATH` | _(자동 다운로드)_ | 로컬 GGUF 폴백 경로 |
| `EMBED_COLLECTION` | `opencrab_vectors_kure` | openai 백엔드 전용 Chroma 컬렉션명(`VECTOR_BACKEND=chroma`일 때) |
| `CHROMA_HOST` / `CHROMA_PORT` | `localhost` / `8000` | docker 모드 Chroma HTTP 서버 주소 |
| `CHROMA_COLLECTION` | `opencrab_vectors` | minilm(local) 임베딩 전용 Chroma 컬렉션명 |
