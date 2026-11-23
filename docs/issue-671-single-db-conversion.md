# 단일 DB 전환 설계 (#671)

이 문서는 저장소 사이 정합성 결함을 구조로 없애기 위한 단계별 전환 설계다. 측정 결과와 판정은 `docs/records/issue-671-single-db-2026-11-18.md`에 있다. 이 문서는 수치를 싣지 않는다. 수치는 기록 문서에서 재현 명령과 함께 읽는다.

시제품은 기본 꺼짐이다. 제품 기본 경로의 동작은 이 변경에서 달라지지 않는다. 라이브 전환은 이 문서의 범위 밖이며 별도 승인을 받는다.

## 1. 전제와 범위

- 결함 묶음의 가장 큰 원인은 한 논리 쓰기가 서로 다른 DB에 따로 커밋되는 구조다. 이 문서는 그 구조를 바꾸는 순서를 정한다.
- 대상 백엔드는 SQLite와 PostgreSQL 둘이다. SQLite는 파일 하나로, PG는 엔진 하나의 트랜잭션 하나로 합친다.
- 결정 규칙: 기록 문서의 기준 1, 2, 3 가운데 하나라도 미충족이면 SQLite 파일 병합(4단계)을 하지 않고 쓰기 경로별 보상 처리 강화로 돌아간다. 미결이나 미측정이 남으면 1단계부터 3단계까지만 진행하고 4단계를 보류한다.
- 1단계, 2단계, PG 쪽 3단계는 파일 병합 판정과 독립이다. SQLite 쪽 3단계 코드는 기본 꺼짐으로 먼저 넣고 4단계에서 켠다.

## 2. 시제품이 만든 것 (이 변경에 포함)

| 항목 | 위치 | 상태 |
|---|---|---|
| 공유 연결과 트랜잭션 구간 | `opencrab/stores/_single_file.py` | 기본 꺼짐. 저장소 생성자의 `shared=` 인자로만 켠다 |
| 저장소 세 곳의 `shared=` 인자 | `local_graph_store.py`, `local_sql_doc_store.py`, `sqlite_vec_store.py` | `shared=None`이면 기존 동작과 같다 |
| `load_chunks`의 단일 트랜잭션 경로 | `opencrab/pack/load.py` | 세 저장소가 같은 공유 파일을 가질 때만 켜진다 |
| 병합 도구 | `scripts/merge_sqlite_stores.py` | 운영자 도구. `--apply` 없이는 계획만 출력한다 |
| 강제 중단 시험 도구 | `scripts/probe_single_db_crash.py` | 합성 데이터만 쓴다 |
| 측정 도구 | `scripts/bench_single_db.py` | 시험 사본만 대상으로 한다. 임베딩 호출 없음 |

Settings와 factory 연결은 시제품에 넣지 않았다. 3단계에서 넣는다(3.1절).

## 3. 단계

다섯 단계의 순서는 고정이다. 앞 단계의 받아들임 기준을 통과하기 전에 다음 단계를 시작하지 않는다.

### 단계 1. 축 이름 정리

- 내용: builder가 축을 `self._neo4j`, `self._mongo`처럼 백엔드 이름으로 부른다. 역할 이름(graph, doc, vector, registry)으로 바꾼다. 로그 문구와 설정 키의 옛 이름은 별칭으로 남긴다.
- 이유: 이름 정리와 백엔드 폐기를 한 변경에 넣으면 범위가 넓어져 검토가 어렵다. 이름을 먼저 정리하면 5단계의 삭제가 기계적이 된다.
- 받아들임 기준:
  - 동작 변경 0. 기존 시험 전량이 같은 결과로 통과한다.
  - `git grep`으로 축 변수 이름에 남은 백엔드 이름이 없다(설정 별칭과 마이그레이션 도구는 예외로 목록에 적는다).
  - 쓰기 주체 목록 시험(`tests/test_write_sink_inventory.py`)이 그대로 통과한다.
- 롤백: 이름만 바뀐 변경이라 `git revert` 하나로 되돌린다. 데이터 변경이 없다.

### 단계 2. 저장소 메서드에 트랜잭션 인자

- 내용:
  - SQLite: 시제품의 `SharedSqliteFile`과 `unit_of_work`를 정식화한다. 저장소 생성자의 `shared=`가 이미 그 입구다.
  - PG: 저장소 메서드가 선택 인자 `conn`을 받는다. 인자가 없으면 지금처럼 메서드 안에서 `begin()`한다.
  - 합류 규칙(시제품에서 시험으로 고정):
    - 구간 밖의 `_tx()`는 기존 동작이다.
    - 구간 안의 `_tx()`와 `_tx(immediate=True)`는 SAVEPOINT에 합류한다.
    - 구간 안의 `_tx(exclusive=True)`는 `RuntimeError`다.
    - 합류한 `_tx()`는 commit도 WAL 체크포인트도 하지 않는다. 바깥 구간이 commit 뒤에 체크포인트를 한 번 한다.
  - 구간 안에서 부르면 안 되는 것: `node_bm25_verify`(조건 없는 `BEGIN`). `_node_bm25_try_drain`은 구간 안에서 건너뛴다. 그래프 `snapshot_path` 경로는 구간 안에서 부르지 않는다. 이 마지막 규칙의 `RuntimeError` 거부는 시제품에 없다. 2단계에서 넣는다.
  - registry(`SQLStore`, SQLAlchemy): 연결 공유는 SQLAlchemy `creator`로 공유 연결을 주입해야 한다. pysqlite의 `BEGIN` 처리와 얽힐 위험이 있다. 선행 프로브로 공유 연결 위에서 세션의 commit과 rollback이 바깥 SAVEPOINT와 어떻게 얽히는지 확인한다. 프로브가 통과하기 전에는 registry를 구간에 넣지 않는다.
- 받아들임 기준:
  - 인자를 넘기지 않는 모든 호출은 기존 동작과 같다. 기존 시험 전량이 통과한다.
  - 인자를 넘긴 경로가 바깥 트랜잭션에 합류한다. 바깥을 롤백하면 안쪽 쓰기가 모두 사라진다(일반 표, vec0, fts5 각각 시험).
  - vec0 청크 경계와 fts5 pending flush를 넘는 큰 배치를 한 구간에 쓰고 롤백해도 행 수와 `integrity-check`가 유지된다.
  - registry 프로브 결과를 문서에 적는다. 위험이 해소되지 않으면 registry는 구간 밖에 두고 쓰기 순서로 보상한다고 적는다.
- 롤백: 인자가 선택이라 호출자가 넘기지 않으면 변경이 없다. 문제가 생기면 `git revert`한다.

### 단계 3. 쓰기 주체별 단일 트랜잭션 전환

쓰기 주체는 `tests/test_write_sink_inventory.py`가 강제하는 목록이 전부다. 이 순서로 하나씩 옮긴다. 주체마다 한 PR이다.

1. 청크 적재(`load_chunks`)의 `flush`와 `flush_single`: 시제품이 이 경로를 구현했다. `load_chunks_incremental`은 같은 패턴이므로 이어서 옮긴다.
2. `write_source`: doc과 vector를 한 구간에 넣는다.
3. builder의 `add_node`와 `add_edge`: best-effort fan-out을 "모두 커밋하거나 모두 롤백"으로 바꾼다. registry가 들어가는 순서는 2단계 프로브의 결과를 따른다.
4. `delete_pack`: graph, doc, vector 삭제를 한 구간에 넣고 삭제 저널은 구간 안의 마지막 쓰기로 남긴다. 저널 재개 경로(`resume=True`)는 PG 쪽에서 계속 필요하다.

#### 3.1 설정과 factory 연결

- `Settings`에 단일 파일 경로 설정을 둔다. 기본은 비어 있고 비어 있으면 지금의 다섯 파일 구조다.
- factory는 경로가 있으면 `SharedSqliteFile`을 하나 만들어 graph, doc, vector 저장소에 `shared=`로 넘긴다. 경로가 없으면 지금처럼 만든다.
- `load_chunks` 같은 쓰기 주체는 저장소가 같은 공유 파일을 가졌는지 `is_single_file`로 판정한다. 설정을 직접 읽지 않는다.

#### 3.2 임베딩 위치

- 시제품은 임베딩을 구간 안에서 부른다(`SqliteVecStore.upsert_texts`가 `_embed`를 쓰기 구간 안에서 부른다). 구간이 임베딩 시간만큼 쓰기 락을 쥔다. 측정 결과는 기록 문서의 쓰기 중 읽기 절에 있다.
- 대안: 구간 밖에서 임베딩을 먼저 계산하고 쓰기만 구간에 넣는다. 이 대안은 측정하지 않았다. 3단계 착수 때 같은 측정 도구로 두 형태를 비교해 고른다.
- 건별 재시도는 건마다 임베딩을 다시 돈다. 원격 임베딩이면 한 건의 결함이 배치 임베딩 비용을 다시 부른다. 임베딩을 구간 밖에서 한 번만 계산하면 이 비용도 사라진다.

#### 3.3 캐시 무효화

- BM25 posting 캐시와 ANN 캐시는 `PRAGMA data_version`으로 신선도를 본다. 단일 파일에서는 graph, vector, registry 어느 쓰기에도 이 값이 오른다. 캐시가 더 자주 무효가 된다. 비용은 기록 문서의 `vecgraph` 쓰기 실행에서 본다.
- ANN 캐시 무효화 시점은 현재 commit 앞이다. 다른 스레드가 commit 전 스냅숏으로 캐시를 다시 만들 수 있다. 3단계에서 commit 뒤 무효화 훅으로 옮긴다. 기본값(`ann=""`)에는 해당 없다.

- 받아들임 기준(주체마다):
  - 시험 DB의 초기 행을 0으로 두고 doc 쓰기, vector 쓰기, graph 쓰기 각각에 예외를 주입한다. 단일 파일에서는 실패한 건이 어느 축에도 남지 않는다.
  - 같은 시험을 다섯 파일 구조에서 돌려 대조군으로 쓴다. 대조군에서 고아가 재현되지 않으면 시험 결함이다.
  - 강제 중단 시험: 쓰기 도중 SIGKILL 뒤에 고아가 0이다. 죽은 시점이 열린 트랜잭션 안이었음을 증명한다(공허 통과 방지).
  - 쓰기 주체 목록 시험이 통과한다. 호출 위치는 목록이 키로 쓰는 (모듈, 함수) 안에 둔다.
  - 단일 파일 경로가 꺼진 기본 설정에서 기존 시험 전량이 통과한다.
- 롤백: 설정을 비워 다섯 파일 구조로 돌아간다. 4단계 이전에는 데이터 파일이 합쳐지지 않았으므로 데이터 영향이 없다. 코드는 주체별 PR을 `git revert`한다.

### 단계 4. 파일 병합 마이그레이션

운영 데이터를 바꾸는 유일한 단계다. 이 단계는 기록 문서의 기준 1, 2, 3이 모두 충족일 때만 시작한다.

- 절차(운영자 수행, 사용자 승인 필요):
  1. `write.lock`을 잡고 서비스를 정지한다(서비스 매니저로 정지한다).
  2. 다섯 파일 전체를 백업한다. 백업 사본으로 복구 시험을 먼저 한다.
  3. 병합 도구를 계획 모드로 돌려 이름 충돌과 쪽 크기를 확인한다.
  4. 병합 도구를 `--apply --verify`로 돌린다. 출력은 `.partial`로 만들어지고 검증을 통과해야 게시된다. 게시는 파일 fsync, `os.link`로 새 이름 연결과 `.partial` 제거, 디렉터리 fsync 순서이며 기존 대상은 덮어쓰지 않는다. 복사 중에는 `synchronous=OFF`이므로 게시 전 fsync가 내용 내구성을 보장한다. 전원 손실 내구성 자체는 이번 측정에서 시험하지 않았다.
  5. 게시 전후에 실데이터 질의를 한다(아래 받아들임 기준).
  6. 설정에 단일 파일 경로를 넣고 서비스를 시작한다. 원본 다섯 파일은 보존 기간 동안 건드리지 않는다.
- billing: `billing_events`와 `idx_billing_tenant`가 registry와 이름이 겹친다. billing은 #105의 격리 사유도 있어 기본 결정은 분리 유지다. 합치려면 한쪽 테이블 이름을 바꾸고 `BillingHooks`의 이름을 함께 바꾸는 변경이 필요하다. 기록 문서의 billing 판정이 "병합 가능"일 때만 이 변경을 이 단계에 넣는다.
- 병합 검증에서 얻은 교훈: 행 수 일치와 `quick_check`만으로는 부족하다. 실제 스냅숏 병합에서 열 이름이 `rowid`인 vec0 그림자 테이블의 물리 rowid가 바뀌어 벡터 조회가 깨졌다. 행 수와 `quick_check`는 통과했다. 도구는 이제 물리 rowid를 함께 옮기고 vec0 실조회를 검증한다. 새 가상 테이블 종류를 더할 때마다 실조회 검증을 같이 더한다.
- 받아들임 기준:
  - 테이블별 행 수 전량 일치, `quick_check` ok, fts5 `integrity-check` ok.
  - vec0 전역 KNN과 팩 한정 KNN이 원본과 같은 id 순서와 거리를 낸다.
  - 라이브가 아닌 사본에서 질의 해시가 현재 구조와 같다(그래프 한도에 걸린 질의는 크기만 비교한다).
  - 백업, VACUUM, 복구가 기록 문서의 기준 3 상한 안에서 끝난다. 운영 장치의 디스크 여유 하한을 지킨다.
  - 전환 직후 실제 질의로 확인한다. 자동 검증의 green만으로 완료로 보지 않는다.
- 롤백:
  - 전환 직후 읽기 검증 구간에서는 설정을 다섯 파일 경로로 되돌리면 된다. 원본 다섯 파일이 그대로 있다.
  - 단일 파일에 새 쓰기가 들어간 뒤에는 다섯 파일로 자동 복귀하지 않는다. 그 구간의 롤백은 단일 파일 백업으로 복구하는 것이다. 이 한계 때문에 전환 직후에 쓰기 동결 구간을 두고 검증을 끝낸 뒤에 쓰기를 연다.
  - 병합 중단은 `.partial`을 버리는 것이 정상 경로다. 원본은 읽기 전용으로만 열렸다.

### 단계 5. 폐기 백엔드 제거

- 내용: Chroma, MongoDB, Neo4j, Kuzu 경로와 그 의존을 제거한다. 백엔드마다 한 PR이다. 제거 순서는 사용처가 적고 이관 도구가 이미 있는 것부터 정한다.
- `neo4j`, `chromadb`, `pymongo`는 필수 의존이다. 제거하면 local 모드만 쓰는 설치가 가벼워진다. 이관 도구(`scripts/migrate_chroma_to_sqlite_vec.py`, `scripts/migrate_sqlite_to_pg.py`, `scripts/migrate_to_local.py`)는 호환 기간 동안 유지한다.
- 받아들임 기준:
  - 제거한 백엔드의 이름이 코드와 시험에서 남지 않는다(이관 도구와 문서의 이력 설명은 예외로 목록에 적는다).
  - 의존을 뺀 설치에서 게이트 전량이 통과한다.
  - 이관 도구의 시험이 그대로 통과한다.
- 롤백: 백엔드별 PR을 `git revert`한다. 데이터는 이 단계에서 바뀌지 않는다.

## 4. PostgreSQL 설계

- 지금: 네 저장소가 SQLAlchemy 엔진 하나를 공유한다(`_get_pg_engine`). 이 공유는 연결 풀만 나눈다. 저장소가 각자 `begin()`한다.
- 전환: 쓰기 주체가 엔진의 `begin()`을 한 번 소유한다. 그 연결을 저장소 메서드의 `conn` 인자로 내려보낸다. 주체가 끝나면 한 번 commit한다.
- PG는 실패한 문장이 트랜잭션 전체를 중단 상태로 만든다. 부분 실패를 흡수해야 하는 자리는 `begin_nested()` SAVEPOINT를 쓴다. 이 패턴은 `packs` 마이그레이션이 이미 쓴다.
- 임베딩은 트랜잭션 밖에서 먼저 계산한다. PG는 긴 트랜잭션이 커넥션을 쥐는 비용이 SQLite의 쓰기 락과 모양이 다르지만 같은 이유로 밖에서 계산한다.
- PG 시제품은 만들지 않았다. 의미 있는 시제품은 PG 저장소 네 곳이 `conn` 인자를 받는 변경이다. 그 변경이 2단계의 본작업이다. 단일 엔진 트랜잭션의 원자성은 PG의 보장이므로 별도 PoC가 새로 보이는 것이 없다. 2단계에서 인자를 넣은 뒤 같은 예외 주입 시험을 PG에서 돈다. PG 기동은 `docs/gate-recipes.md`의 절차를 따른다.

## 5. 위험과 미해결 항목

| 항목 | 내용 | 처리 단계 |
|---|---|---|
| registry 연결 공유 | SQLAlchemy `creator` 주입과 pysqlite의 `BEGIN` 처리 | 2단계 선행 프로브 |
| `snapshot_path` 구간 안 호출 | 구간 안 호출은 별도 연결을 열어 교착 위험. 시제품은 계약만 문서화했다 | 2단계에서 `RuntimeError` 거부 |
| 락 시간 | 구간이 쓰기 락을 쥔 시간만큼 다른 쓰기 연결이 기다린다. 기록 문서의 쓰기 중 읽기 절에 측정값이 있다 | 3단계 임베딩 위치 결정 |
| 캐시 무효화 범위 | `data_version`은 파일 전체의 쓰기에 오른다 | 3단계 측정 후 결정 |
| `lower` UDF | 그래프 저장소가 등록한 `lower`가 단일 연결에서 다른 저장소에도 보인다. 지금 스키마의 인덱스와 트리거는 `lower`를 쓰지 않는다. `lower` 표현식 인덱스를 만들면 의미가 달라진다 | 새 인덱스 추가 때 점검 |
| 전원 손실 | WAL과 `synchronous=NORMAL`에서 마지막 commit이 사라질 수 있다. 단일 파일은 한 commit이 원자적이므로 축 사이가 어긋난 상태는 남지 않는다. 다섯 파일은 파일마다 commit이 따로 사라질 수 있다. 시험하지 않았다. 분석만 했다 | 3단계 |
| 백업 도구 | `backup_data_dir`는 고정 파일 목록을 쓰고 단일 모드를 거부한다. 단일 모드 백업은 별도 함수 `backup_single_root`이고 CLI가 `SINGLE_SQLITE_FILE`이 있을 때만 이 함수를 부른다. 복원은 `opencrab.stores.single_restore`다(7.4) | B6에서 구현 |
| 쓰기 락 직렬화 | 프로세스 사이 쓰기는 지금도 `write.lock`으로 직렬화된다 | 측정으로 판정 |

## 6. 재현 명령

- 병합 계획: `python scripts/merge_sqlite_stores.py --source-dir <디렉터리> --out <새 파일>`. `--apply --verify`를 붙이면 병합하고 검증한다.
- 강제 중단 시험: `python -m pytest tests/test_single_db_crash.py`.
- 예외 주입 시험: `python -m pytest tests/test_load_chunks_single_file.py`.
- 공유 연결 시험: `python -m pytest tests/test_single_file_store.py`.
- 측정 도구: `python scripts/bench_single_db.py <samples|latency|judge|concurrent|maintenance> --help`.

## 7. factory 연결과 전환 전제

단계 3.1의 factory 연결을 구현한 상태를 적는다. 이 절의 항목은 모두 시험 이름이나 후속 항목 이름을 하나 이상 가리킨다. 시험은 `tests/test_factory_single_file.py`에 있고 괄호 안은 설계 시험 번호다. 각 시험의 설명 문장이 설계 시험 번호로 시작하므로 번호로 찾을 수 있다.

### 7.1 동작

- `SINGLE_SQLITE_FILE`이 비어 있으면 모든 factory가 다중 파일 구조를 그대로 만든다(T-N1 `test_single_setting_empty_uses_multi_file_paths`).
- 설정이 있으면 graph, doc, vector, sql factory가 한 `SharedSqliteFile`을 나눠 쓴다. 객체는 경로별 등록부가 하나만 만들고 결속 검증(대장, manifest, marker, metadata 행)을 거친 뒤 등록한다(T-N2 `test_single_four_stores_share_one_file`, T-N4, T-N5, T-G1).
- billing factory는 등록부에 결속된 열린 SQL 저장소가 있을 때만 root의 `billing.db`를 연다(T-N3, T-E11).
- 대장 state가 authorized가 아니면 DB를 열기 전에 거부한다. root에 `-wal`과 `-shm`이 생기지 않는다(T-E1, T-G3).

### 7.2 전환 전제

1. 게시된 root의 대장 state가 authorized여야 한다. ready나 staged는 거부된다(T-E1). 게시 도구는 대장을 ready까지만 올린다. 병합 도구의 `authorize` 하위 명령이 ready를 authorized로 한 번 올린다. 사본 시험은 `python scripts/merge_sqlite_stores.py authorize --root <게시 root> --run-kind copy-test --verification-pass <독립 검증 PASS 파일> --copy-test-run-id <식별자> --apply`로, 운영은 `--run-kind production --verification-pass <독립 검증 PASS 파일> --copy-test-pass <사본 시험 PASS 파일> --apply`로 실행한다. `--apply`가 없으면 점검만 하고 root를 바꾸지 않는다. 이 명령은 PASS 파일의 존재와 형태만 본다. 성공하면 사본 시험 실행은 독립 검증 PASS 파일 하나의 sha256을, 운영 실행은 두 PASS 파일의 sha256을 stderr의 `authorize applied` 줄에 찍는다. 그 값을 실행 기록과 대조하는 일은 호출자의 몫이다.
2. 대장, manifest, marker, metadata 행이 서로 맞아야 한다. 어긋나면 거부된다(T-E2).
3. `STORAGE_MODE=local`이고 sqlite-vec 벡터 백엔드이며 `SINGLE_SQLITE_FILE`이 root 안 `single.db`의 절대 경로여야 한다(T-E3 `test_single_rejects_bad_settings`).
4. 설정의 `LOCAL_DATA_DIR`과 환경변수 `LOCAL_DATA_DIR`이 모두 root와 같아야 한다. `write.lock`과 `billing.db.lock`과 삭제 저널 디렉터리가 root 아래에 놓이기 때문이다(T-E3).
5. 이전 데이터 디렉터리의 `packs/`와 삭제 저널을 root로 옮기는 절차는 아직 없다(후속 `followup_671_switch_data_dir_move`).
6. 단일 모드에서 `write.lock`을 쥔 스레드가 factory를 직접 처음 부르면 수명 잠금 순서 위반으로 거부된다(T-E9 `test_single_first_call_under_write_lock_is_rejected`). `write.lock`을 factory보다 먼저 잡는 진입인 MCP 쓰기 도구와 `crabharness` 적용은 `hold_single_file`이 `write.lock` 앞에서 공유 파일을 열고 결속을 판정한다. 그래서 첫 쓰기 호출이 서고 미승인 root는 `write.lock`이 생기기 전에 거부된다(`tests/test_single_mode_auth_lock_order.py`의 T12-N6, N7, E1, G1).
7. `SINGLE_SQLITE_FILE` 없이 단일 root(`ledger.json`이나 `single.db`가 있는 디렉터리)를 `LOCAL_DATA_DIR`로 받은 프로세스는, root에 파일을 만들거나 DB를 열기 전에 거부된다. 거부 지점은 다섯 파일 factory와 직접 생성자, chroma 분기, bootstrap-on-empty, 백업(데이터 디렉터리와 목적지 둘 다), `migrate_chroma_to_sqlite_vec.py`, `migrate_to_local.py`, `import_obsidian_vault.py`, 그리고 `LOCAL_DATA_DIR`을 필수 인자나 기본값으로 받아 root 아래에 쓰거나 `write.lock`을 잡는 스크립트 여섯(`repair_store_mismatch_499.py`, `cleanup_openclaw_heartbeat_493.py`, `export_pack_graph_from_neo4j.py`, `import_pack_graph_to_neo4j.py`, `build_nemotron_personas_korea_pack.py`, `backfill_space.py`)과 CLI 명령 둘(`packs backfill-pack-id --apply`, `packs source-fts-orphans --apply`), `crabharness`의 승격 적용(`apply_promotion_package`)이다. `crabharness` 적용은 `SINGLE_SQLITE_FILE`이 없을 때만 이 거부를 지난다. 단일 모드에서는 root의 `write.lock`이 정상 구성원이고, `hold_single_file`이 `write.lock` 앞에서 결속을 판정한다. 두 CLI 명령은 단일 모드에서 `SINGLE_SQLITE_FILE is set` 문구로 `write.lock` 앞에서 거부한다. 두 명령은 root 안의 옛 `graph.db`와 `doc_store.db`로 계획을 만드는데, 단일 모드의 factory는 단일 파일 위의 저장소를 돌려주므로 그 계획을 단일 그래프에 쓰는 오사용이 되기 때문이다(T12-E5). query, ingest, serve stdio는 단일 모드에서 인증을 단일 DB로 통과한다. 거부된 경우 root는 바뀌지 않는다. 출력 경로를 명시해야 쓰는 스크립트와 읽기 전용 스크립트는 root 아래에 쓰지 않으므로 이 거부와 무관하다(전수 판정은 설계 문서 6.2와 6.3). 그래서 롤백은 root를 치운 뒤에 환경 설정을 고친다(7.4). 팩토리와 생성자 경로의 검사는 열려는 DB 파일의 바로 위 디렉터리만 보고, 출력 경로를 기본값으로 받는 세 스크립트(`export_pack_graph_from_neo4j.py`, `import_pack_graph_to_neo4j.py`, `build_nemotron_personas_korea_pack.py`)는 그 경로의 모든 상위 디렉터리를 본다. 거부 사유는 `single SQLite root requires SINGLE_SQLITE_FILE`이다(`tests/test_five_file_single_root_guard.py`). 이 거부와 root 불변은 게시한 이름만 있는 root에 대한 약속이다. 옛 DB 파일이 섞인 혼합 root에서는 읽기 전용 경로가 거부 없이 `-wal`과 `-shm`을 만들 수 있다. 대상은 `packs backfill-pack-id`와 `packs source-fts-orphans`의 dry-run, 그리고 `mode=ro`로 여는 스크립트다. 그 보호는 이 단위의 범위 밖이다(설계 문서 BR11과 U-B11-7). 운영 런북은 옛 데이터를 `.pre671`로 옮긴 뒤 root를 게시하므로 운영 root는 혼합이 되지 않는다.

### 7.3 지원하지 않는 기능

- 단일 모드의 사용자 생성: `opencrab init`은 `.env`를 만들기 전에 거부하고, bootstrap-on-empty는 읽기 전용이라 사용자를 만들지 않는다. 단일 root의 사용자와 토큰은 병합 입력에서 온다. 인증 세 경로(stdio와 CLI의 `require_local_principal`, `maybe_bootstrap_on_empty`, HTTP 토큰 조회)는 단일 DB를 읽는다. 열기 실패는 HTTP에서 503과 `Retry-After`다(`tests/test_single_mode_auth_lock_order.py`의 T12-N2, N3, N4, E3, E8).
- backfill과 `backup_data_dir`의 직접 호출: 단일 모드를 거부한다(T-E8 a와 b, 후속 `followup_671_backup_backfill_single`). `backfill_space`, `migrate_to_local`, `reconcile_doc_graph_nodes`가 단일 모드에서 조용히 백업을 얻지 않게 하려는 거부다. 단일 모드 백업은 CLI `python -m opencrab.stores.backup --to <목적지>`가 `backup_single_root`로 만든다. 세트에는 `single.db`, `billing.db`, `MANIFEST.json`만 들어가고 `--exclude`는 거부된다. 삭제 흔적(`pack_delete_operations` 행이나 `delete-pack-journals` 파일)이 있으면 삭제 저널 단위가 준비될 때까지 거부한다.
- 관리 CLI: `packs backfill-pack-id --apply`와 `packs source-fts-orphans --apply`는 단일 모드에서 명시 문구로 거부한다(T-E8 c와 d, T12-E5). user와 token 관리 명령은 단일 모드를 모른다(후속 `followup_671_admin_cli_single`).
- 삭제, fork, 증분 노드 정리: `pack_delete_operations` 표가 있으면 거부한다(T-N7 `test_single_write_gates_stay_fail_closed`와 `test_single_delete_and_fork_gates_stay_fail_closed`). 삭제 저널 단위(후속 `followup_671_delete_journal_unit`)가 이 거부를 풀고, 그때 `test_single_file_delete_reserves_before_external_journal`의 xfail을 지운다.
- node BM25 색인이 backfill 상태이거나 검색 중 drain이 필요한 경우: 단일 모드에서 진행하지 못하거나 작업 단위 밖에서 쓴다(T-N2가 ready 상태 게시 DB만 허용한다. 후속 `followup_671_bm25_backfill_single`, `followup_671_bm25_drain_single`).
- 공유 객체 없이 단일 파일을 여는 경로: 보호 검사(`reject_unmanaged_single_file`)가 표식 표가 있으면 거부한다. 다른 연결이 파일을 잠가 읽지 못해도 파일 헤더로 저널 모드를 가려 판정하거나 거부한다. WAL 파일은 기존 연결과 같은 대기 뒤에도 읽지 못하면 BUSY를 올리고, 롤백 저널 파일은 잠금 없는 읽기로 표식만 판정하며, 판정할 수 없으면 거부한다. 롤백 DB에 hot journal이 남았으면 rw 연결로 저널을 회복한 뒤 판정한다(시험 `test_unmanaged_check_refuses_locked_marker_db`, `test_unmanaged_check_refuses_locked_wal_db`, `test_locked_marker_db_is_not_opened_even_when_lock_is_released_after_the_check`, `test_unmanaged_check_undeterminable_is_refused`, `test_unmanaged_check_recovers_hot_journal_rollback_db`). 잔여 위험은 여섯이다. 첫째, 롤백 파일의 잠금 없는 읽기(immutable)는 merge 부분 파일이면 틀린 결과를 낼 수 있다(후속 `followup_671_partial_marker_order`). 게시된 단일 파일은 늘 WAL이라 이 경로에 닿지 않는다(`test_published_single_db_is_wal`). 둘째, 관리 밖에서 단일 파일 사본을 롤백 모드로 바꾼 파일에도 같은 오판 가능성이 있다. 이 사본은 게시 보장 밖이다. 셋째, 롤백 DB가 스키마를 바꾸는 중에 잠겨 있으면 잠금 없는 읽기가 틀린 결과를 낼 수 있다. 넷째, 저널을 회복하는 창에서 잠금 없는 읽기가 틀린 결과를 낼 수 있다. 판정 뒤 헤더를 다시 읽어 모드 전환은 막지만 이 창은 막지 못한다. 다섯째, hot journal 회복 재시도는 저널 회복이라는 쓰기를 한다. finalize 중에 종료해 hot journal이 남은 `.partial`에 이 재시도가 닿으면 merge가 보존하라고 한 `.partial`의 바이트를 바꾼다. 여섯째, 회복 재시도가 BUSY와 LOCKED가 아닌 코드로 실패하면 HTTP 인증 조회는 503이 아니라 500이다.
- 한 공유 객체 위에서 저장소를 동시에 만드는 일: 훅을 등록하는 생성은 factory의 레지스트리 잠금 안에서 한 번에 하나씩 일어난다(T12-N8). 작업 단위 안의 저장소 생성은 그 잠금을 잡기 전에 거부한다(T12-G8, G10). 남은 한계는 둘이다. 열린 공유 객체를 받은 뒤 소유자를 등록하기 전에 다른 스레드가 마지막 소유자를 풀면 등록이 실패한다. 쓰기 도구의 첫 호출이 `write.lock` 안에서 컨텍스트를 만드는 기존 순서 반전은 비단일 모드와 같이 남는다.

### 7.4 롤백

순서는 다음 넷이다. 첫째, 서비스를 멈춘다. 둘째, 게시 root를 보존 이름으로 옮긴다. 7.2의 5번에서 root로 옮긴 `packs/`와 저널도 전환 전 디렉터리로 되돌린다. 셋째, `SINGLE_SQLITE_FILE`을 비우고 `LOCAL_DATA_DIR`을 전환 전 데이터 디렉터리로 되돌린다. 두 설정을 함께 바꾼다. 넷째, 서비스를 시작한다. root를 옮기기 전에 설정만 바꾸면 안 된다. `SINGLE_SQLITE_FILE`만 비우고 `LOCAL_DATA_DIR`이 root인 채로 두면 다섯 파일 factory가 root를 거부한다(7.2의 7번). 그 거부는 root를 바꾸지 않으므로 서비스는 뜨지 않고 root에는 빈 파일이 생기지 않는다. 단일 모드에서 쓴 데이터는 이전 파일에 없다. 그러므로 이 롤백은 단일 모드에서 새로 쓰기 전까지만 데이터 손실이 없다. 설정이 비어 있는 경로는 코드를 되돌리지 않아도 이전과 같다(T-N1). authorized 상태는 되돌리는 명령이 없다. 게시 root를 되돌릴 때는 root를 보존 이름으로 옮긴다.

단일 모드에서 쓴 뒤 단일 파일을 롤백 지점으로 되돌릴 때는 복원 명령을 쓴다. 롤백 지점은 `python -m opencrab.stores.backup --to <root 밖 목적지>`가 만든 세트다. 서비스와 임시 인스턴스를 멈추고 `single.db`, `billing.db`와 각 `-wal`, `-shm`의 `fuser` 출력이 0줄인 것을 확인한 뒤 다음을 실행한다.

```bash
python -m opencrab.stores.single_restore <세트 디렉터리>            # dry-run(기본): 점검과 계획만 출력하고 root를 바꾸지 않는다
python -m opencrab.stores.single_restore <세트 디렉터리> --apply    # root의 두 파일을 세트로 되돌린다
```

- 종료 코드: 0 전부 통과, 1 사전 점검 거부(root 그대로), 2 적용 중 오류(중간 상태이므로 사람이 보존 이름과 임시 이름을 보고 판단), 3 이전 적용의 잔재 이름 감지(root 그대로).
- 되돌리기 전에 옛 `single.db`, `billing.db`와 곁파일(`-wal`, `-shm`, `-journal`)을 지우지 않고 `<이름>.restore-saved-<적용 id>`로 남긴다. 임시 복사본은 `<이름>.restore-tmp-<적용 id>`다. 보존 이름이 하나라도 root에 있으면 재복원은 rc 3으로 거부한다. 보존 이름을 확인해 치우거나 옮긴 뒤 다시 실행한다.
- 교체 순서는 `single.db` 보존 링크를 가장 먼저 걸고 `single.db` rename을 가장 나중에 한다. 그 사이 중단되면 live `single.db`의 링크 수가 2라 factory 경유 열기가 `file must be regular and have one link`로 거부된다. 이 거부는 의도한 닫힌 실패다.
- 점검(`integrity_check`와 표별 행 수)은 독점 수명 잠금 아래에서 하고 결속 검증은 잠금을 푼 뒤에 한다. 계획 문서의 `quick_check` 순서와 다르다.
- 복원 도구는 `openclaw-conversations/automation`과 `cursor_state.json`을 건드리지 않는다. 7단계 사본의 되돌림은 운영 절차가 맡는다.

### 7.5 재현 명령

- factory 단일 모드 시험: `python -m pytest tests/test_factory_single_file.py tests/test_factory_storage_modes.py`.
- authorize 하위 명령 시험: `python -m pytest tests/test_authorize_single_root.py`.
- 이 절의 문서 점검: 저장소 루트에서 `rg -nP` 패턴으로 머신 경로와 커밋 해시를 찾아 0건(종료 코드 1)임을 확인한다.
