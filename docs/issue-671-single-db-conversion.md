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

- BM25 posting 캐시는 `PRAGMA data_version`으로 신선도를 본다. 단일 파일에서는 graph, vector, registry 어느 쓰기에도 이 값이 오르므로 캐시가 더 자주 무효가 된다. 비용은 기록 문서의 `vecgraph` 쓰기 실행에서 본다. ANN 캐시는 `data_version`이 바뀌어도 벡터 표의 `max(rowid)`가 같으면 유지한다(9.3 D).
- ANN 캐시 무효화는 commit 뒤로 옮겼다. 쓰기 구간 안의 쓰기는 `after_commit` 콜백으로 캐시를 비우고 세대를 올리며, 세대 비교가 빌드 중 commit이 남기는 낡은 캐시의 설치를 막는다(9.3 E).

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
| 캐시 무효화 범위 | `data_version`은 파일 전체의 쓰기에 오른다. ANN 캐시는 벡터 `max(rowid)`로 걸러 영향이 없다 | BM25 캐시는 3단계 측정 후 결정, ANN 캐시는 9절 |
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
5. 이전 데이터 디렉터리의 `packs/`, `openclaw-conversations/`, `datasets/` 세 항목을 root로 옮기는 절차는 8.6에 있다. 삭제 저널은 옮기지 않고 옛 디렉터리에 둔다.
6. 단일 모드에서 `write.lock`을 쥔 스레드가 factory를 직접 처음 부르면 수명 잠금 순서 위반으로 거부된다(T-E9 `test_single_first_call_under_write_lock_is_rejected`). `write.lock`을 factory보다 먼저 잡는 진입인 MCP 쓰기 도구와 `crabharness` 적용은 `hold_single_file`이 `write.lock` 앞에서 공유 파일을 열고 결속을 판정한다. 그래서 첫 쓰기 호출이 서고 미승인 root는 `write.lock`이 생기기 전에 거부된다(`tests/test_single_mode_auth_lock_order.py`의 T12-N6, N7, E1, G1).
7. `SINGLE_SQLITE_FILE` 없이 단일 root(`ledger.json`이나 `single.db`가 있는 디렉터리)를 `LOCAL_DATA_DIR`로 받은 프로세스는, root에 파일을 만들거나 DB를 열기 전에 거부된다. 거부 지점은 다섯 파일 factory와 직접 생성자, chroma 분기, bootstrap-on-empty, 백업(데이터 디렉터리와 목적지 둘 다), `migrate_chroma_to_sqlite_vec.py`, `migrate_to_local.py`, `import_obsidian_vault.py`, 그리고 `LOCAL_DATA_DIR`을 필수 인자나 기본값으로 받아 root 아래에 쓰거나 `write.lock`을 잡는 스크립트 여섯(`repair_store_mismatch_499.py`, `cleanup_openclaw_heartbeat_493.py`, `export_pack_graph_from_neo4j.py`, `import_pack_graph_to_neo4j.py`, `build_nemotron_personas_korea_pack.py`, `backfill_space.py`)과 CLI 명령 둘(`packs backfill-pack-id --apply`, `packs source-fts-orphans --apply`), `crabharness`의 승격 적용(`apply_promotion_package`)이다. `crabharness` 적용은 `SINGLE_SQLITE_FILE`이 없을 때만 이 거부를 지난다. 단일 모드에서는 root의 `write.lock`이 정상 구성원이다. `hold_single_file`이 `write.lock` 앞에서 결속을 판정한다. 두 CLI 명령은 단일 모드에서 `SINGLE_SQLITE_FILE is set` 문구로 `write.lock` 앞에서 거부한다. root에 옛 `graph.db`나 `doc_store.db`가 없으면 두 명령은 그 거부보다 먼저 `graph.db not found`나 `doc_store.db not found`로 끝난다. 이때도 root는 바뀌지 않는다. 두 명령은 root 안의 옛 `graph.db`와 `doc_store.db`로 계획을 만든다. 단일 모드의 factory는 단일 파일 위의 저장소를 돌려준다. 그 계획을 실행하면 계획을 단일 그래프에 쓰게 된다. `SINGLE_SQLITE_FILE is set` 거부는 그 오사용을 막는다(T12-E5). query, ingest, serve stdio는 단일 모드에서 인증을 단일 DB로 통과한다. `crabharness` CLI의 `promotion-apply`는 `SINGLE_SQLITE_FILE`이 없는 단일 root에서 인증 단계(`require_local_principal`)가 먼저 `No local user is bootstrapped` 오류로 끝나고 root는 바뀌지 않는다. 쓰기 거부는 라이브러리 호출자(`apply_promotion_package`)를 막는다. 거부된 경우 root는 바뀌지 않는다. 출력 경로를 명시해야 쓰는 스크립트와 읽기 전용 스크립트는 root 아래에 쓰지 않으므로 이 거부와 무관하다(인자 처리는 각 스크립트가 정하고 `tests/test_five_file_single_root_guard.py`가 거부 지점을 고정한다). 그래서 롤백은 root를 치운 뒤에 환경 설정을 고친다(7.4). 팩토리와 생성자 경로의 검사는 열려는 DB 파일의 바로 위 디렉터리만 본다. 출력 경로를 기본값으로 받는 세 스크립트(`export_pack_graph_from_neo4j.py`, `import_pack_graph_to_neo4j.py`, `build_nemotron_personas_korea_pack.py`)는 그 경로의 모든 상위 디렉터리를 본다. 거부 사유는 `single SQLite root requires SINGLE_SQLITE_FILE`이다(`tests/test_five_file_single_root_guard.py`). 이 거부와 root 불변은 옛 DB 파일(`graph.db`, `doc_store.db` 등)이 없는 root에 대한 약속이다. 옛 DB 파일이 섞인 혼합 root에서는 읽기 전용 경로가 거부 없이 `-wal`과 `-shm`을 만들 수 있다. 대상은 `packs backfill-pack-id`와 `packs source-fts-orphans`의 dry-run, 그리고 `mode=ro`로 여는 스크립트다. 그 보호는 이 단위의 범위 밖이다(같은 시험 파일의 `test_t11_e10_mixed_directory_with_wal_graph_db_gets_no_sidecars`는 표식 검사 경로 하나가 곁파일을 만들지 않음만 고정한다. 나머지 읽기 전용 경로의 곁파일 생성은 시험으로 고정되지 않았다). 운영 런북(8.6)은 옛 데이터 디렉터리를 root 밖의 `.pre671` 이름 디렉터리로 두고 root에는 세 항목만 옮기므로 운영 root에는 옛 DB 파일이 없다. 이 상태는 재인입 서비스가 옛 `graph.db`에 직접 연결하지 않을 때만 유지되며 그 대응은 별도 변경이다.

### 7.3 지원하지 않는 기능

- 단일 모드의 사용자 생성: `opencrab init`은 `.env`를 만들기 전에 거부한다. bootstrap-on-empty는 읽기 전용이라 사용자를 만들지 않는다. 단일 root의 사용자와 토큰은 병합 입력에서 온다. 인증 세 경로(stdio와 CLI의 `require_local_principal`, `maybe_bootstrap_on_empty`, HTTP 토큰 조회)는 단일 DB를 읽는다. 열기 실패는 HTTP에서 503과 `Retry-After`다(`tests/test_single_mode_auth_lock_order.py`의 T12-N2, N3, N4, E3, E8).
- backfill과 `backup_data_dir`의 직접 호출: 단일 모드를 거부한다(T-E8 a와 b, 후속 `followup_671_backup_backfill_single`). `backfill_space`, `migrate_to_local`, `reconcile_doc_graph_nodes`가 단일 모드에서 조용히 백업을 얻지 않게 하려는 거부다. 단일 모드 백업은 CLI `python -m opencrab.stores.backup --to <목적지>`가 `backup_single_root`로 만든다. 세트에는 `single.db`, `billing.db`, `MANIFEST.json`만 들어가고 `--exclude`는 거부된다. 삭제 흔적(`pack_delete_operations` 행이나 `delete-pack-journals` 파일)이 있으면 삭제 저널 단위가 준비될 때까지 거부한다.
- 관리 CLI: `packs backfill-pack-id --apply`와 `packs source-fts-orphans --apply`는 단일 모드에서 명시 문구로 거부한다(T-E8 c와 d, T12-E5). user와 token 관리 명령은 단일 모드를 모른다(후속 `followup_671_admin_cli_single`).
- 삭제, fork, 증분 노드 정리: `pack_delete_operations` 표가 있으면 거부한다(T-N7 `test_single_write_gates_stay_fail_closed`와 `test_single_delete_and_fork_gates_stay_fail_closed`). 삭제 저널 단위(후속 `followup_671_delete_journal_unit`)가 이 거부를 푼다. 그때 `test_single_file_delete_reserves_before_external_journal`의 xfail을 지운다.
- node BM25 색인이 backfill 상태이거나 검색 중 drain이 필요한 경우: 단일 모드에서 진행하지 못하거나 작업 단위 밖에서 쓴다(T-N2가 ready 상태 게시 DB만 허용한다. 후속 `followup_671_bm25_backfill_single`, `followup_671_bm25_drain_single`).
- 공유 객체 없이 단일 파일을 여는 경로: 보호 검사(`reject_unmanaged_single_file`)가 표식 표가 있으면 거부한다. 다른 연결이 파일을 잠가 읽지 못해도 파일 헤더로 저널 모드를 가려 판정하거나 거부한다. WAL 파일은 기존 연결과 같은 대기 뒤에도 읽지 못하면 BUSY를 올린다. 롤백 저널 파일은 잠금 없는 읽기로 표식만 판정한다. 판정할 수 없으면 거부한다. 롤백 DB에 hot journal이 남았으면 rw 연결로 저널을 회복한 뒤 판정한다. 이 예외를 그대로 받는 곳은 검사 함수를 직접 부르는 스크립트와 단일 모드 billing이다. 다섯 파일 저장소 생성자(graph, doc, vector, SQL)는 판정할 수 없으면 예외를 올리지 않고 파일을 열지 않은 채 그 저장소만 비가용으로 둔다. 표식이 있거나 단일 root여서 확정한 거부는 생성자도 그대로 올린다. 비가용 저장소를 만들 때 write.lock과 billing의 own-file 잠금은 main과 같이 생길 수 있다. 본파일 바이트와 표는 바뀌지 않고, WAL 파일에서는 검사가 `-wal`과 `-shm`을 만들 수 있다(main도 연결로 만든다). 거부된 SQL 저장소는 `_engine`을 가지며 새 연결 때마다 검사를 다시 하므로, 잠금이 풀린 뒤에도 표식 있는 파일에는 쓰지 않는다(시험 `test_unmanaged_check_refuses_locked_marker_db`, `test_unmanaged_check_refuses_locked_wal_db`, `test_locked_marker_db_is_not_opened_even_when_lock_is_released_after_the_check`, `test_unmanaged_check_undeterminable_is_refused`, `test_unmanaged_check_recovers_hot_journal_rollback_db`, `test_five_file_store_with_busy_wal_marker_db_is_unavailable`, `test_refused_marked_billing_file_is_not_written_after_lock_release`, `test_context_is_built_when_one_five_file_store_is_unavailable`). 잔여 위험은 일곱이다. 첫째, 롤백 파일의 잠금 없는 읽기(immutable)는 merge 부분 파일이면 틀린 결과를 낼 수 있다(후속 `followup_671_partial_marker_order`). 게시된 단일 파일은 늘 WAL이라 이 경로에 닿지 않는다(`test_published_single_db_is_wal`). 둘째, 관리 밖에서 단일 파일 사본을 롤백 모드로 바꾼 파일에도 같은 오판 가능성이 있다. 이 사본은 게시 보장 밖이다. 셋째, 롤백 DB가 스키마를 바꾸는 중에 잠겨 있으면 잠금 없는 읽기가 틀린 결과를 낼 수 있다. 넷째, 저널을 회복하는 창에서 잠금 없는 읽기가 틀린 결과를 낼 수 있다. 판정 뒤 헤더를 다시 읽어 모드 전환은 막지만 이 창은 막지 못한다. 다섯째, hot journal 회복 재시도는 저널 회복이라는 쓰기를 한다. finalize 중에 종료해 hot journal이 남은 `.partial`에 이 재시도가 닿으면 merge가 보존하라고 한 `.partial`의 바이트를 바꾼다. 여섯째, 회복 재시도가 BUSY와 LOCKED가 아닌 코드로 실패하면 검사 함수를 직접 부르는 스크립트가 그 예외를 그대로 받는다. 다섯 파일 MCP HTTP 인증 조회에서는 그 예외도 판정 불가로 기록되어 503 store-unavailable이 된다. 일곱째, BUSY나 읽기 실패로 판정하지 못한 저장소는 그 프로세스 동안 비가용이다. main은 잠금이 그 뒤 풀리면 가용이 될 수 있다. 롤백 저널 파일의 BUSY는 검사가 0.2초만 기다리고 main은 5초 기다린다. 로컬 저장소의 ping류(가용 여부를 보지 않고 연결을 쓰는 경로)는 잠금이 풀린 뒤에도 거부를 다시 올리나 main은 연결된다. MCP HTTP 인증 조회는 로그 이유가 store-busy에서 store-unavailable로 바뀐다. 거부된 SQL 저장소의 새 연결은 표식이 없을 때만 만들어지며, 표식이 있으면 RuntimeError, 판정 불가이면 sqlite 오류다.

  main의 billing SQL 저장소는 `billing.db`가 열리면 available이 True다. 열리지 않으면 available이 False다. 이 변경은 표식 검사가 sqlite 오류나 OS 오류로 그 파일을 판정하지 못하면 False다(전수 목록이 아닌 예: 헤더나 스키마를 읽지 못하는 손상 파일, 읽기를 막는 잠금이 걸린 WAL 파일, `-wal`이나 `-shm`을 만들거나 열 수 없는 WAL 파일, 회복하지 못한 hot journal이 남은 롤백 파일). 이 차이는 거부 기록이 있으면 파일을 열지 않기 때문이며 설계가 받아들인 차이다. 데이터 디렉터리나 그 상위 디렉터리를 탐색할 수 없으면 이 변경에서는 생성자가 예외를 올린다(#700). 잠겨 있기만 한 롤백 저널 `billing.db`는 main과 이 변경 둘 다 available이 True다.

  REST 인증 조회는 예외를 종류별로 분류하므로 잠금(BUSY와 LOCKED) 중에는 store-busy 503을 그대로 남기고 그 밖의 코드는 main과 같이 500이다.
- 한 공유 객체 위에서 저장소를 동시에 만드는 일: 훅을 등록하는 생성은 factory의 레지스트리 잠금 안에서 한 번에 하나씩 일어난다(T12-N8). 작업 단위 안의 저장소 생성은 그 잠금을 잡기 전에 거부한다(T12-G8, G10). 남은 한계는 둘이다. 열린 공유 객체를 받은 뒤 소유자를 등록하기 전에 다른 스레드가 마지막 소유자를 풀면 등록이 실패한다. 쓰기 도구의 첫 호출이 `write.lock` 안에서 컨텍스트를 만드는 기존 순서 반전은 비단일 모드와 같이 남는다.

### 7.4 롤백

순서는 다음 넷이다. 첫째, 서비스를 멈춘다. 둘째, 8.6에서 root로 옮긴 세 항목을 전환 전 디렉터리로 되돌리고 게시 root를 보존 이름으로 옮긴다. `<root>.intent.json`과 `<root>.lifetime.lock`도 같은 접미사의 보존 이름으로 옮긴다. intent가 남으면 다음 게시가 `publication artifacts already exist`로 거부된다. 순서의 정본은 8.6의 롤백 항목이다. 셋째, `SINGLE_SQLITE_FILE`을 비운다. 8.6처럼 전환 전 디렉터리를 원래 이름으로 되돌렸으면 `LOCAL_DATA_DIR` 값은 바꾸지 않는다. 되돌리지 않았으면 `LOCAL_DATA_DIR`을 전환 전 데이터 디렉터리로 바꾼다. 넷째, 서비스를 시작한다. root를 옮기기 전에 설정만 바꾸면 안 된다. `SINGLE_SQLITE_FILE`만 비우고 `LOCAL_DATA_DIR`이 root인 채로 두면 다섯 파일 factory가 root를 거부한다(7.2의 7번). 그 거부는 root를 바꾸지 않으므로 서비스는 뜨지 않고 root에는 빈 파일이 생기지 않는다. 단일 모드에서 쓴 데이터는 이전 파일에 없다. 그러므로 이 롤백은 단일 모드에서 새로 쓰기 전까지만 데이터 손실이 없다. 설정이 비어 있는 경로는 코드를 되돌리지 않아도 이전과 같다(T-N1). authorized 상태는 되돌리는 명령이 없다. 게시 root를 되돌릴 때는 root를 보존 이름으로 옮긴다.

단일 모드에서 쓴 뒤 단일 파일을 롤백 지점으로 되돌릴 때는 복원 명령을 쓴다. 롤백 지점은 `python -m opencrab.stores.backup --to <root 밖 목적지>`가 만든 세트다. 서비스와 임시 인스턴스를 멈추고 `single.db`, `billing.db`와 각 `-wal`, `-shm`의 `fuser` 출력이 0줄인 것을 확인한 뒤 다음을 실행한다.

```bash
python -m opencrab.stores.single_restore <세트 디렉터리>            # dry-run(기본): 점검과 계획만 출력하고 root를 바꾸지 않는다
python -m opencrab.stores.single_restore <세트 디렉터리> --apply    # root의 두 파일을 세트로 되돌린다
```

- 종료 코드: 0은 전부 통과다. 1은 보존 링크를 거는 단계 앞의 거부다(설정 읽기, 사전 점검, 수명 잠금, `write.lock`, 임시 복사). root의 두 파일은 그대로다. 임시 복사가 실패한 뒤 임시 이름 정리까지 실패한 드문 경우에만 `.restore-tmp-*` 이름이 남는다(#692). 2는 root가 바뀌었을 수 있는 오류다. 교체 중 오류, 교체 뒤 점검(무결성과 행 수) 불일치, `write.lock` 구간의 예외, 교체 뒤 결속 검증 실패, 예상하지 못한 실패가 여기에 든다. 사람이 보존 이름과 임시 이름을 보고 판단한다. 3은 이전 적용이 남긴 `.restore-tmp-*`나 `.restore-saved-*` 이름을 root에서 발견한 거부이고 root는 그대로다.
- 되돌리기 전에 옛 `single.db`, `billing.db`와 곁파일(`-wal`, `-shm`, `-journal`)을 지우지 않고 `<이름>.restore-saved-<적용 id>`로 남긴다. 임시 복사본은 `<이름>.restore-tmp-<적용 id>`다. 보존 이름이나 임시 이름이 하나라도 root에 있으면 재복원은 rc 3으로 거부한다. 그 이름을 확인해 치우거나 옮긴 뒤 다시 실행한다.
- 교체 순서는 `single.db` 보존 링크를 가장 먼저 걸고 `single.db` rename을 가장 나중에 한다. 그 사이 중단되면 live `single.db`의 링크 수가 2라 factory 경유 열기가 `file must be regular and have one link`로 거부된다. 이 거부는 의도한 닫힌 실패다.
- 점검(`integrity_check`와 표별 행 수)은 독점 수명 잠금 아래에서 하고 결속 검증은 잠금을 푼 뒤에 한다. 계획 문서의 `quick_check` 순서와 다르다.
- 복원 도구는 `openclaw-conversations/automation`과 `cursor_state.json`을 건드리지 않는다. 8.6 P7의 롤백 지점 사본의 되돌림은 운영 절차가 맡는다.

### 7.5 재현 명령

- factory 단일 모드 시험: `python -m pytest tests/test_factory_single_file.py tests/test_factory_storage_modes.py`.
- authorize 하위 명령 시험: `python -m pytest tests/test_authorize_single_root.py`.
- 이 절의 문서 점검: 저장소 루트에서 `rg -nP` 패턴으로 머신 경로와 커밋 해시를 찾아 0건(종료 코드 1)임을 확인한다.
- 8절의 문서 점검: 같은 `rg -nP` 명령을 8절 범위에도 적용해 0건(종료 코드 1)임을 확인한다.

## 8. 게시 중단 복구와 전환 런북

이 절은 운영자가 따르는 절차다. 7절은 코드 동작과 시험 근거를 적고 8절은 사람이 하는 일을 적는다. 모든 경로의 `<root 부모>`와 `<root 이름>`은 실제 값으로 바꿔 쓴다. 한 줄에 단순 명령 하나만 실행한다.

### 8.1 산출물 이름과 잠금

게시 단계에 쓰는 이름은 다음과 같다.

| 이름 | 위치 | 만드는 때 | 사라지는 때 |
|---|---|---|---|
| `<세트 이름>.source.lifetime.lock` | 원본 세트의 상위 디렉터리 | 운영자가 게시 전에 만든다 | 운영자가 지운다 |
| `<root>.lifetime.lock` | root의 부모 | 게시 도구가 잠글 때 만든다 | 남는다(다음 게시가 다시 잠근다). 8.6 P7 롤백에서 보존 이름으로 옮긴다 |
| `<root>.intent.json` | root의 부모 | 게시 도구가 만든다(state는 preparing, publishing, ready 순서) | 게시 도구는 지우지 않는다. 게시가 끝나면 ready로 남고 실패하면 그때의 state로 남는다. 8.4 복구와 8.6 롤백에서 보존 이름으로 옮긴다 |
| `<root>.intent.json.tmp` | root의 부모 | 게시 도구가 intent를 publishing이나 ready로 바꿀 때 만든다 | rename으로 사라진다. 그 사이에 게시 도구가 죽거나 쓰기가 실패하면 남는다 |
| `<root>.staging` | root의 부모 | 게시 도구가 만든다 | 이름이 root로 바뀐다 |
| `<root>` | root의 부모 | staging이 이름을 바꿔 생긴다 | 8.4 복구와 8.6 P7 롤백에서 보존 이름으로 옮긴다 |
| `ledger.json.tmp` | root 안 | 게시 도구가 ledger를 ready로 바꿀 때와 `authorize`가 ledger를 authorized로 바꿀 때 만든다 | rename으로 사라진다. 그 사이에 게시 도구나 authorize가 죽거나 쓰기가 실패하면 남는다 |
| `billing.db`, `ledger.json`, `manifest.json`, `marker.json`, `single.db`, `single.db.lifetime.lock` | root 안 | 게시 도구가 만든다(여섯 이름이 게시한 파일 전부다) | root와 함께 움직인다 |

### 8.2 게시 준비

1. 원본 세트를 검증한다. 이 명령은 병합 원본 세트(다섯 파일 형식)에만 쓴다. 종료 코드 0과 `OK` 줄이 나와야 한다.

```bash
python -m opencrab.stores.backup --verify <세트 디렉터리>
```

2. 원본 잠금 파일을 만든다. 파일은 비어 있는 일반 파일이고 링크 수가 1이어야 한다. 병합 도구는 이 파일을 만들지 않고 읽기 전용으로 열어 공유 잠금만 건다. 아래 명령은 파일이 이미 있으면 덮어쓰지 않고 `cannot overwrite existing file`과 rc 1로 멈춘다.

```bash
bash -c 'set -o noclobber; : > <세트 상위 디렉터리>/<세트 이름>.source.lifetime.lock'
stat -c '%F %s %h' <세트 상위 디렉터리>/<세트 이름>.source.lifetime.lock
```

`stat` 출력은 `regular empty file 0 1`이어야 한다.

3. 증거 파일을 만든다. `scripts/make_publication_evidence.py <세트 디렉터리> --out <증거 파일> --preparer-id <PREPARER> --frozen-from <UTC 시각> --packs-checked-at <UTC 시각> --frozen-until <UTC 시각>`이 세트의 `MANIFEST.json`에서 만들고 병합 도구의 규칙 함수로 검사한다. 규칙을 어기면 파일을 만들지 않고 rc 2로 끝난다. 증거 파일은 세트 디렉터리 밖에 둔다. 손으로 쓸 때의 키는 13개다: `version`, `snapshot_id`, `source_manifest_sha256`, `preparer_id`, `frozen_from`, `frozen_until`, `writer_stopped`, `billing_same_snapshot`, `packs_observation`, `backup_at`, `packs_checked_at`, `capture_from`, `capture_until`. 값의 관계는 다음과 같다.
   - `version`은 1이다. `preparer_id`는 비어 있지 않은 문자열이다. `snapshot_id`는 세트 `MANIFEST.json`의 `set_id`와 같다. `source_manifest_sha256`은 `MANIFEST.json` 바이트의 sha256이다.
   - `frozen_from` <= `capture_from` <= `capture_until` <= `frozen_until`이고 `packs_checked_at`은 `frozen_from`과 `frozen_until` 사이다.
   - `backup_at`은 `MANIFEST.json`의 `created_at`과 같고 `capture_until`은 `created_at`에 1초를 더한 시각보다 앞선다.
   - 시각은 모두 `Z`나 `+00:00` 같은 UTC 오프셋을 명시해 쓴다. 오프셋이 없거나 UTC가 아니면 증거 검사가 거부한다. `writer_stopped`와 `billing_same_snapshot`은 true이고 `packs_observation`은 `"absent"`다.
4. 증거 경로 규칙: authorize의 증거 인자(`--verification-pass`, `--copy-test-pass`)는 절대 경로로 쓰고 경로에 쉼표를 넣지 않는다. `authorize applied` 줄은 경로를 쉼표로 이어 찍으므로 쉼표가 있으면 실행 기록과의 대조가 갈라진다.
5. 병합 도구를 `--apply` 없이 먼저 돌려 계획을 본다. `--apply`가 없으면 계획 모드이고 이 모드는 정상이어도 `publication mode is not ready`와 rc 2로 끝난다.

### 8.3 정상 완료 상태

`ledger authorized`와 `intent ready`의 조합이 정상 완료 상태다. 런타임이 root를 연 뒤에는 병합 도구의 `authorize` 재실행이 `root file set differs`로 거부되는 것이 정상이다. 열기 경로에 따라 `single.db-wal`과 `single.db-shm`이 생겨 root의 항목 집합이 달라지기 때문이다. 이 상태를 재실행으로 확인하지 않는다.

factory를 거치지 않고 `single.db`를 여는 경로가 있다(`opencrab/stores/_sqlite_base.py`, `opencrab/stores/sql_store.py`와 일부 스크립트). 이 경로는 `mode=ro`로 열어도 `-wal`과 `-shm` 곁파일을 만들 수 있다.

### 8.4 게시 중단 복구

정본 절차는 하나다. 남은 산출물을 보존 이름으로 옮기고 병합을 처음부터 다시 돌린다. 보존 이름은 `<이름>.aborted-<UTC 시각>`이고 한 번의 복구에서 네 이름 모두 같은 접미사를 쓴다. 접미사는 운영자가 한 번 정해 이후 명령에 그대로 적는다.

1. 바깥 잠금 확인. 바깥 잠금은 게시 도구만 잡고 런타임은 잡지 않는다. 안쪽 잠금은 런타임이 공유로 쥔다. 먼저 `ls -d <root 부모>/<root 이름>.lifetime.lock`을 실행한다. 파일이 없으면 flock을 실행하지 않는다(flock이 빈 잠금 파일을 만든다). 잠금 파일이 없는데 다른 산출물이 있으면 멈추고 담당자에게 알린다. 파일이 있으면 `flock -n <root 부모>/<root 이름>.lifetime.lock true`를 실행한다. rc 0이면 게시 도구가 살아 있지 않다. rc 1이면 다른 게시 도구가 잠금을 쥔 것이므로 기다리고 아무것도 옮기지 않는다. 잠금 파일이 없고 `<root>`, `<root>.staging`, `<root>.intent.json`, `<root>.intent.json.tmp`도 모두 없으면 옮길 것이 없으므로 5단계로 간다.
2. 읽기 전용 상태 읽기. `ls -d`로 `<root>`, `<root>.staging`, `<root>.intent.json`, `<root>.intent.json.tmp`의 존재를 적는다. root가 있으면 `ls -A <root>`로 `ledger.json.tmp`의 존재를 적는다. 존재하는 intent와 root의 `ledger.json`은 `cat`으로 읽어 state 값을 적는다. 파일을 열어 고치지 않는다.
3. 분기. root 안에 `ledger.json.tmp`가 있고 root ledger가 ready이며 intent가 ready이면 authorize가 중간에 죽은 것이다. 이때는 4로 가지 않고 8.5를 따른다. `<root>`, `<root>.staging`, `<root>.intent.json`, `<root>.intent.json.tmp`가 모두 없으면 옮길 것이 없으므로 5단계로 간다. 그 밖에서: root ledger가 authorized이고 intent가 ready이면 8.3의 정상 상태이므로 끝낸다. root ledger가 ready이고 intent가 ready이면 게시는 끝났으므로 authorize 단계(7.2의 1번)로 간다. 그 밖(`<root>.staging`이 있음, root ledger가 staged, intent가 preparing이나 publishing, intent tmp가 있음)은 4로 간다. ledger나 intent가 읽히지 않거나 state 값이 알려진 값이 아니거나 authorized인데 intent가 ready가 아닌 경우는 멈추고 읽은 내용을 담당자에게 알린다.
4. 보존 이름으로 옮기기. 존재하는 것만 같은 접미사로 이 순서로 옮긴다: root, staging, intent, intent tmp. 옮기기 전에 각 이름마다 두 가지를 확인한다. 하나, `stat -c %d <root 부모> <원래 이름>`이 같은 값을 두 줄로 낸다. 둘, `ls -d <보존 이름>`이 0줄이다(`mv -T`는 같은 이름의 빈 디렉터리 위로 조용히 덮어쓰므로 이 확인이 필요하다). 1단계의 `flock -n`은 확인 뒤 잠금을 곧바로 푼다. 그래서 복구하는 동안에는 병합과 authorize를 실행하지 않는다. 각 이동은 `flock -n <root 부모>/<root 이름>.lifetime.lock mv -T <원래 이름> <보존 이름>` 한 줄로 실행해 바깥 잠금을 쥔 채 옮긴다. 이 명령이 rc 1이면 다른 게시 도구가 잠금을 쥔 것이므로 멈춘다. stderr에 `mv:` 메시지가 있으면 잠금이 아니라 mv의 실패이므로 메시지를 읽고 멈춘다. rc가 0이 아니면 이유와 관계없이 멈춘다. root 안의 `ledger.json.tmp`는 root와 함께 옮겨진다. `<root>.lifetime.lock`은 옮기지 않는다. 다음 게시가 그 파일을 다시 잠근다.
5. 원본 세트 확인. 8.2의 검증 명령을 다시 돌려 통과를 본다. 원본 세트와 증거 파일은 게시가 읽기 전용으로만 열었으므로 바뀌지 않았어야 한다.
6. 같은 인자로 병합을 처음부터 다시 돌린다(계획 모드 확인 뒤 `--apply --verify --immutable-source`). 부분 재개는 없다.
7. 끝난 뒤 root ledger ready와 intent ready를 확인하고 authorize 단계로 간다. 보존 이름은 자동으로 지우지 않는다. 지울 때는 원본 세트와 증거 파일이 남아 있어 다시 만들 수 있음을 확인하고 담당자의 승인을 받는다. 디스크 여유 하한에 걸려 재실행이 거부되면 보존 staging부터 승인을 받아 지운다.

게시 도구의 종료 코드는 다음과 같다.

- rc 0 `publication ready`: 7단계로 간다.
- rc 1 `publication staging failed:` 또는 `publication publish failed:`: 중간 산출물이 남는다. 1단계부터 따른다.
- rc 2: 두 갈래다. `publication preflight failed:` 앞에 `publication publish failed:` 줄이 없으면 root는 만들어지지 않았다. 그 줄이 먼저 있으면 게시 단계가 실패한 뒤 원본 파일이나 원본 잠금의 재확인 또는 정리가 실패한 것이고 root가 남았을 수 있다. 다만 intent를 쓴 뒤의 실패(여유 공간 부족, staging 생성 실패)는 `<root>.intent.json`(state preparing)과 `<root>.staging`을 남길 수 있다. 그래서 rc와 메시지에 관계없이 1단계부터 따른다. 메시지 없이 traceback으로 끝난 rc 1도 같다. 계획 모드(`--apply` 없음)는 정상이어도 `publication mode is not ready`와 rc 2로 끝난다.
- rc 2 `publication ready; cleanup failed` 또는 `publication ready; post-ready source check failed`: root는 이미 유효하다. 재실행하지 않고 메시지의 원인만 처리한다.

### 8.5 authorize 중단 복구

8.2의 4번 규칙대로 증거 경로는 절대 경로이고 쉼표가 없어야 한다. `authorize`가 죽었을 때는 `ledger.json`을 읽어 아래 표로 판단한다. 요약하면 ledger가 authorized면 완료다. ready면 `ledger.json.tmp`를 지우고 다시 실행한다. `ledger.json.tmp`는 `ledger.json`이 ready임을 읽은 뒤에만 지운다.

| 죽은 지점 | root ledger | `ledger.json.tmp` | 다음 실행 결과 | 조치 |
|---|---|---|---|---|
| 점검 중 | ready | 없음 | 정상 실행 | 없음 |
| 임시 파일을 만든 뒤 쓰기 전 | ready | 빈 파일 | `root file set differs`로 거부 | ledger가 ready임을 확인하고 임시 파일을 지운 뒤 다시 실행 |
| 임시 파일 fsync 뒤 rename 전 | ready | 완전한 새 본문 | `root file set differs`로 거부 | 위와 같다 |
| rename 뒤 디렉터리 fsync 전 | authorized(페이지 캐시) | 없음 | 재부팅하지 않았으면 `ledger state differs`로 거부. 재부팅했으면 ready나 authorized 중 하나 | ledger를 읽어 확인한다. authorized면 완료. ready면 다시 실행 |
| 디렉터리 fsync 뒤 재읽기 전 | authorized | 없음 | `ledger state differs`로 거부 | 완료로 본다. factory 열기로 확인 |
| 쓰기 실패 rc 1 (`not performed` 또는 쓰기 실패) | ready | 남음(부분 또는 완전) | `root file set differs`로 거부 | 출력의 observed ledger state가 ready임을 확인하고 임시 파일을 지운 뒤 다시 실행 |
| 쓰기 실패 rc 1 (`took effect`) | authorized | 없음 | `ledger state differs`로 거부 | 완료로 본다. observed ledger state가 authorized임을 확인하고 factory 열기로 확인 |
| 디렉터리 교체 감지 rc 1 | 옮겨진 원래 디렉터리의 ledger는 authorized. 현재 이름의 디렉터리는 그대로 | 없음 | observed 값은 현재 이름의 디렉터리 기준 | 두 디렉터리를 확인하고 정본 root를 정한 뒤 다시 실행 |
| 완료(런타임이 root를 열기 전) | authorized | 없음 | `ledger state differs`로 거부 | 없음 |
| 완료(런타임이 root를 연 뒤) | authorized | 없음 | `root file set differs`로 거부(`single.db-wal`과 `single.db-shm`이 생겨 항목 집합이 달라진다. 런타임이 정상 종료해 곁파일이 사라지면 `ledger state differs`) | 정상 상태다. 재실행으로 확인하지 않는다 |

어느 지점에서도 `ledger.json`은 옛 본문 전체이거나 새 본문 전체다. 부분 본문은 임시 파일에만 생긴다.

### 8.6 옛 데이터 이동과 롤백

이 소절은 서비스를 멈춘 구간에서 옛 데이터 디렉터리의 세 항목을 게시 root로 옮기고 되돌리는 절차다. 옛 데이터 디렉터리를 root 밖에서 `<이름>.pre671`이라는 이름으로 두는 것이 이 운영 절차의 약속이다. 저장소 코드는 이 이름을 쓰지 않는다. 이동 대상은 `packs/`, `openclaw-conversations/`, `datasets/` 셋이다. 삭제 저널은 옛 디렉터리에 둔다. 아래 항목에 팩 이름, 건수, 머신 경로는 적지 않는다.

- P1 이동 순서. 병합 게시(ready), authorize, 항목 이동, `SINGLE_SQLITE_FILE` 설정, 읽기 검증, 롤백 지점 기록, 쓰기 개방 순이다. 롤백 지점 기록은 7.4의 롤백 지점 세트와 P7의 `openclaw-conversations/automation` 사본을 같은 시점에 만드는 일이다. 이동이 authorize 앞이면 `root file set differs`로 거부된다. 롤백 지점 기록 전에 P5의 임시 인스턴스를 멈춘다. 같은 시점에 `packs/` 파일 목록과 sha256 목록을 남긴다. 사본은 `<root 부모>` 아래 새 디렉터리에 `cp -a`로 뜨고 rc 0과 두 트리의 sha256 목록이 같음을 확인한다. 사본 경로는 롤백 지점 기록에 함께 적는다.
- P2 같은 파일시스템 확인과 이동 방법. 이동 직전에 옛 디렉터리와 root의 장치 번호를 `stat -c %d`로 비교한다. 같으면 `mv -T`로 옮긴다. 같은 장치의 `mv`는 rename 하나이므로 원자적이다. 다르면 중단한다. 다른 장치의 `mv`는 복사 뒤 삭제로 바뀌어 원자성과 정지 시간 계산이 깨진다. 이동 대상 이름이 root에 이미 있으면 `ls -d`로 확인해 덮어쓰지 않고 중단한다. `mv -T`는 같은 이름의 빈 디렉터리 위로 조용히 덮어쓰므로 이 확인이 필요하다.
- P3 이동 전 서비스 정지 확인. 재인입 서비스와 타이머가 inactive인지 `systemctl --user is-active`로 확인한다. 활성이면 이동하지 않는다. 한 항목씩 옮기고 항목마다 이동 뒤 목록을 기록한다.
- P4 이동 전후 기록. 이동 전에 각 이동 항목의 파일 목록(상대 경로와 크기)과 `packs/` 파일의 sha256 목록을 남긴다. 이동 뒤 같은 명령을 root에서 돌려 두 기록이 같음을 확인한다. rename은 내용을 바꾸지 않으므로 어긋나면 다른 프로세스가 건드린 것이고 중단한다.
- P5 읽기 검증. 루프백 임시 인스턴스에서 `content_pack_list`와 `packs list`가 이동 전 `packs/` manifest의 팩 목록을 포함하는지 본다. 기준은 SQL `packs` 행이 있고 읽기 범위 안인 manifest 팩이다. 기준 집합의 누락이 0인지를 보고 건수를 기준으로 삼지 않는다. `content_pack_list`의 query로 manifest 키워드에만 있는 단어를 검색해 그 팩이 돌아오는지도 본다.
- P6 서비스 시작 전 디렉터리 확인. 재인입 단위의 표준 출력 경로의 부모(`root/openclaw-conversations/automation`)가 있는지 확인한다. 없으면 단위가 시작하지 못할 수 있다. 시작 명령 앞에서 늘 확인한다.
- P7 롤백 순서. 서비스 정지 상태를 확인한다. 세 항목을 root에서 옛 디렉터리로 되돌린다(P2와 같은 확인). 디렉터리 이름을 맞바꾼다. `<root>.intent.json`과 `<root>.lifetime.lock`을 같은 접미사의 보존 이름으로 옮긴다. 모든 rename은 대상 이름이 이미 있으면 중단한다. root 잠금 파일(`<root>.lifetime.lock`)이 없으면 중단한다. 설정 출처에서 `SINGLE_SQLITE_FILE` 줄을 지우고 서비스 프로세스 환경에 그 값이 없음을 확인한다. 롤백 접미사는 `.saved-<UTC 시각>` 하나이며 root, intent, 잠금 파일 셋과 쓰기 개방 뒤에 옮기는 `openclaw-conversations/automation`에 같은 값을 쓴다. 쓰기 개방 직전에 7.4의 롤백 지점 세트를 만들 때 같은 시점에 `openclaw-conversations/automation`(`cursor_state.json`이 그 안에 있다)의 사본을 root 밖에 떠 둔다. 쓰기 개방 뒤 다섯 파일로 돌아갈 때는 위 순서에 더해 옛 디렉터리로 되돌린 `openclaw-conversations/automation`을 그 사본으로 바꾼다. 이 단계를 빼면 커서가 앞서 있어 재인입이 롤백 지점 뒤의 메시지를 건너뛴다. 쓰기 개방 뒤의 MCP 쓰기는 다섯 파일에 없으므로 사라진다. 단일 모드에 남아 롤백 지점으로 되돌릴 때는 7.4의 복원 명령을 쓰고 같은 사본을 함께 되돌린다. 사본으로 바꿀 때는 먼저 지금의 `openclaw-conversations/automation`을 같은 롤백 접미사의 보존 이름으로 옮기고 그다음 사본을 그 자리로 옮긴다. 이 두 이동도 P2와 같은 확인을 하고 대상 이름이 이미 있으면 중단한다. 다섯 파일로 돌아갈 때는 이 교체를 디렉터리 이름을 맞바꾸기 전에 한다. 사본은 root와 같은 파일시스템에 둔다. 지금의 `openclaw-conversations/automation`이 옮겨 가는 보존 이름은 root 밖 `<root 부모>/automation.saved-<UTC 시각>`이고 시각은 롤백 접미사와 같은 값이다. 항목 이동(P1의 항목 이동 단계) 전에 롤백하면 세 항목을 옛 디렉터리로 되돌리는 단계를 건너뛴다. 이동을 시작했으면 root에 있는 이동 항목만 되돌린다. root가 없거나 8.4 복구로 보존 이름으로 옮겨졌으면 root 쪽 이름 바꾸기를 건너뛰고 `.pre671` 디렉터리만 기본 경로로 옮긴다. 재실행하지 않고 다섯 파일로 돌아가기로 정하면 8.4의 1~4단계로 남은 산출물을 보존 이름으로 옮긴 뒤 이 순서를 한다. 게시를 시작하지 않아 잠금 파일이 없으면 위의 '잠금 파일이 없으면 중단'을 적용하지 않는다.
- P8 외부 경로 확인. `grep -rn 'data/localcrab' <외부 스크립트 디렉터리>`를 다시 실행해 이동 항목 셋 밖의 기본 경로를 고정 문자열로 쓰는 외부 스크립트가 있는지 확인한다. 새로 나온 경로가 있으면 이동 대상에 넣을지 정한 뒤에 이동한다.
- P9 경로 조건 확인. 기본 경로와 그 부모의 `realpath`가 자기 자신과 같은지, 기본 경로가 마운트 지점이 아닌지 확인한다(`findmnt -n <경로>`가 출력을 내지 않아야 한다). 마운트 지점이면 그 디렉터리를 rename할 수 없다. 사본 시험 전에 한 번, 전환 당일에 한 번 더 한다. 다르면 게시가 거부되므로 시작하지 않는다.
- P10 `build_nemotron_personas_korea_pack.py`의 기능 제한. 이 스크립트는 단일 모드에서 쓸 수 없다. 단일 root를 가리키면 거부되고 root 밖 데이터 디렉터리로 돌리면 다섯 파일 구조의 별도 작업이라 운영 root에 반영되지 않는다. 이 스크립트가 만든 stage 산출물만 P3의 방법으로 `root/packs/`에 옮길 수 있다.

기본 출력 경로가 `packs/` 아래인 오프라인 스크립트 셋(`export_pack_graph_from_neo4j.py`, `import_pack_graph_to_neo4j.py`, `build_nemotron_personas_korea_pack.py`)은 단일 root 안에서 거부가 유지된다. 이 스크립트 셋을 스위치 뒤에 다시 쓰려면 root 밖 경로로 실행하고 완성된 산출물만 옮긴다.

### 8.7 설정 순서

환경 설정은 root를 치운 뒤에 고친다. api와 gateway 서비스 유닛의 `EnvironmentFile` 줄을 주석 처리하고 재시작하는 롤백은 금지한다. root를 둔 채 그렇게 하면 다섯 파일 factory가 root를 거부해 서비스가 뜨지 않는다(7.2의 7번). 단일 모드에서는 사용자를 만들지 않으며 사용자와 토큰은 병합 입력에서 온다(7.3의 첫 항목).

### 8.8 PASS 파일 두 개 마련

`authorize --run-kind production`은 PASS 파일 두 개를 받는다(7.2의 1번). 도구는 두 파일의 존재와 형태만 본다. 내용은 해석하지 않는다. 그래서 파일이 무엇을 증명하는지는 마련하는 사람이 정한다. 아래는 이 저장소가 쓰는 마련 방법이다.

| 파일 | 담는 내용 | 마련 시점 |
|---|---|---|
| 독립 검증 PASS(`--verification-pass`) | 변경 대상 커밋을 새 컨텍스트에서 독립 검증한 판정 원문 | 병합 대상 커밋이 정해진 뒤, 사본 시험 전 |
| 사본 시험 PASS(`--copy-test-pass`) | 운영 데이터의 일관된 사본으로 8.9절 전체를 돌린 결과에 대한 독립 검증 판정 원문 | 사본 시험을 끝낸 뒤, 운영 전환 전 |

마련 규칙은 다음과 같다.

1. 판정 원문을 한 글자도 고치지 않고 일반 파일 하나에 저장한다. 심볼릭 링크와 빈 파일은 거부된다.
2. 두 파일은 서로 다른 파일이어야 한다. 같은 파일을 두 인자에 넣으면 거부된다.
3. 파일은 게시 root 밖에 둔다. root 안에 있으면 거부된다.
4. 절대 경로를 쓰고 경로에 쉼표를 넣지 않는다(8.2의 4번).
5. 저장한 뒤 `sha256sum`으로 각 파일의 해시를 읽어 실행 기록에 적는다. 판정이 가리키는 대상 커밋도 함께 적는다.
6. `authorize --apply`가 stderr의 `authorize applied` 줄에 찍은 해시 둘이 기록과 같은지 대조한다. 다르면 전이를 무효로 보고 롤백 경로로 간다. 이 대조는 도구가 하지 않는다.
7. 판정 대상 커밋이 운영에 배포할 커밋과 같은지 배포 전에 확인한다. 어긋나면 판정을 새로 받는다.

사본 시험 PASS를 받으려면 사본 시험 자체를 8.9절대로 끝까지 돌려야 한다. 사본 시험은 `--run-kind copy-test --verification-pass <PASS1> --copy-test-run-id <COPY_RUN_ID>`로 authorize한다. 운영 데이터를 읽는 구간은 읽기 전용으로 하고 사본만 쓴다.

### 8.9 일반화 실행표

이 표는 한 운영 환경에서 다섯 파일 데이터 디렉터리를 단일 root로 바꾸는 순서를 변수로 적는다. 8.1~8.7이 동작과 복구의 정본이고 이 표는 그 순서를 한 줄에 단순 명령 하나로 이어 붙인다. 표의 명령은 합성 데이터 디렉터리 하나로 끝까지 돌려 확인했다. 확인 방법은 이 절 끝에 있다. 한 줄에 단순 명령 하나만 실행하고, 반복문, 파이프, `&&`는 쓰지 않는다. 합격 기준을 어기면 같은 단계에서 멈추고 아래 갈래를 따른다.

#### 변수

| 변수 | 뜻 |
|---|---|
| `<DATA_PARENT>` | 데이터 디렉터리의 부모. root와 같은 파일시스템이어야 한다 |
| `<ROOT>` | 게시 root. 전환 전 데이터 디렉터리와 같은 경로다(`<DATA_PARENT>/<ROOT 이름>`) |
| `<OLD>` | 전환 전 데이터 디렉터리의 보존 이름(`<ROOT>.pre671`). 저장소 코드는 이 이름을 쓰지 않는다 |
| `<REPO>` | localcrab 체크아웃 경로 |
| `<PY>` | 운영 가상환경의 python |
| `<ENVFILE>` | 서비스 단위가 읽는 환경 파일. 값은 열지 않고 키 이름만 읽는다 |
| `<UNIT_FRONT>`, `<UNIT_PROXY>`, `<UNIT_GW>`, `<UNIT_API>` | 외부 노출, 인증 프록시, MCP 게이트웨이, REST 앱 단위. 이름에 `.service`를 붙인다(차단 디렉터리 `<RT>/<단위>.d`의 이름이 된다) |
| `<UNIT_REINGEST>`, `<TIMER_REINGEST>` | 대화 재인입 서비스와 타이머(없으면 이 줄을 건너뛴다) |
| `<SETA>`, `<SETC>` | 2단계의 첫 백업 세트와 병합 원본 세트 디렉터리 이름 |
| `<T0>`, `<T1>`, `<T2>` | 증거 파일의 `frozen_from`, `packs_checked_at`, `frozen_until`(UTC 오프셋 명시) |
| `<PASS1>`, `<PASS2>` | 독립 검증 PASS와 사본 시험 PASS의 절대 경로(8.8) |
| `<TMP>` | 병합이 임시 파일을 쓸 디렉터리(`<DATA_PARENT>/sqlite-tmp`). root와 같은 파일시스템이고 tmpfs가 아니다 |
| `<RT>` | systemd 사용자 런타임 단위 디렉터리(`$XDG_RUNTIME_DIR/systemd/user`). 1단계의 시작 차단 파일을 둔다 |
| `<TMP_GW>`, `<TMP_PROXY>`, `<TMP_API>` | 6단계 임시 단위 이름. 운영 단위와 다른 이름이다 |
| `<GW_PORT>`, `<PROXY_PORT>`, `<API_PORT>`, `<GW_PORT_운영>` | 임시 게이트웨이, 임시 프록시, 임시 REST 앱의 루프백 포트와 운영 게이트웨이 포트. 임시 포트는 운영이 쓰지 않는 값이다 |
| `<토큰 변수>` | 읽기 검증 도구에 넘길 토큰이 든 환경변수 이름. 값은 명령줄에 쓰지 않는다 |
| `<기준 팩 목록 파일>` | 0단계에서 서비스가 도는 동안 `--save-expected`로 만든 팩 id 목록 파일. 저장소에 올리지 않는다 |
| `<홈 디렉터리>`, `<운영 단위와 같은 PATH>` | 임시 게이트웨이의 `WorkingDirectory`와 `PATH`. 운영 단위의 값을 `systemctl --user show`로 읽어 쓴다 |
| `<ROOT 이름>` | `<ROOT>`의 마지막 경로 이름(intent 파일은 `<DATA_PARENT>/<ROOT 이름>.intent.json`) |
| `<PREPARER>` | 증거 파일을 만든 사람이나 절차의 이름(`--preparer-id` 값) |
| `<COPY_RUN_ID>` | 사본 시험 authorize의 실행 식별자(8.8) |
| `<서버 이름>`, `<클라이언트 설정 파일>` | `--client-config` 파일의 `mcpServers` 키와 그 파일 경로 |
| `<UTC 시각>` | 롤백 접미사 `.saved-<UTC 시각>`의 값. 롤백 한 번에 하나를 정해 root, intent, 잠금 파일, `automation`에 같은 값을 쓴다(8.6 P7) |
| `<대상 커밋>`, `<롤백 세트 이름>` | 1b에서 배포할 커밋, 7단계 백업이 낸 세트 디렉터리 이름 |
| `<게이트웨이 MainPID>` | 8단계에서 `show -p MainPID --value`로 읽은 값. 프록시와 REST 앱은 각 단위의 값이다 |
| `<DATA 경로 문자열>`, `<외부 스크립트 디렉터리>` | 5단계에서 외부 스크립트가 기본 경로를 고정 문자열로 쓰는지 찾을 때의 경로 문자열과 스크립트 디렉터리 |
| `<단위>` | 갈래 A에서 차단 파일을 지울 단위(`<UNIT_FRONT>`, `<UNIT_PROXY>`, `<UNIT_GW>`, `<UNIT_API>` 중 하나) |
| `<RUN>` | 실행 기록 디렉터리. 데이터 디렉터리 밖에 둔다 |
| `<HEAVY>` | 무거운 실행을 한 번에 하나씩 돌리는 래퍼(없으면 빈 값). 운영 장치의 메모리 상한을 지킨다 |

서비스 매니저가 systemd 사용자 유닛이 아니면 `systemctl --user` 줄을 그 매니저의 같은 뜻의 명령으로 바꾼다. 매니저가 관리하는 프로세스는 `kill`로 죽이지 않는다.

#### 0단계 사전 조건과 기록 (정지 전)

서비스가 아직 도는 동안 기준 팩 목록 파일을 만든다. 이 도구의 `ontology_query`는 billing 행을 더한다. 정지 전에 한 번만 돌린다.

```bash
<PY> <REPO>/scripts/check_read_endpoints.py <GW_PORT_운영> mcp --token-env <토큰 변수> --save-expected <기준 팩 목록 파일>
```

- 합격: `RESULT: PASS`와 rc 0이다. 파일이 이미 있으면 도구가 거부한다. 이 파일은 팩 id 목록이므로 저장소에 올리지 않는다.

```bash
mkdir -p <RUN>
date -Is
sha256sum <PASS1>
sha256sum <PASS2>
git -C <REPO> rev-parse HEAD
git -C <REPO> status --porcelain
```

- 합격: 점검 시각이 백업과 정기 작업(백업 서비스, 재인입 타이머)의 시각과 겹치지 않는다. 두 PASS 해시와 판정 대상 커밋을 `<RUN>`에 적었다. 현재 HEAD(되돌림 지점)를 적었다. `status --porcelain`이 0줄이다.
- 실패: 정지하지 않는다. 시각을 다시 정한다.

```bash
stat -c '%F %h %n' <ROOT>/opencrab.db <ROOT>/graph.db <ROOT>/doc_store.db <ROOT>/vectors.db <ROOT>/billing.db
stat -c '%d %n' <ROOT> <DATA_PARENT>
realpath <ROOT>
realpath <DATA_PARENT>
findmnt -n <ROOT>
findmnt -n <DATA_PARENT>
findmnt -n -o FSTYPE -T <DATA_PARENT>
ls -d <ROOT>/ledger.json <ROOT>/single.db <OLD>
ls -A <ROOT>
df -B1 --output=avail <DATA_PARENT>
```

- 합격: 다섯 파일이 일반 파일이고 링크 수가 1이다. 장치 번호가 같다. `realpath`가 자기 자신이다. `findmnt`가 `<ROOT>`와 `<DATA_PARENT>`에 출력을 내지 않는다(마운트 지점이면 rename이 안 된다. 8.6 P9). `<DATA_PARENT>`의 파일시스템 종류가 tmpfs가 아니다(`<TMP>`는 3단계에서 `<DATA_PARENT>` 아래에 만든다). `ledger.json`, `single.db`, `<OLD>`가 없다. `ls -A <ROOT>`의 최상위 이름이 8.6의 이동 대상 세 항목과 다섯 DB, 곁파일, 잠금과 삭제 저널, 백업 세트 같은 알려진 이름뿐이다. 모르는 이름이 있으면 처리 방법을 정한 뒤 시작한다. 디스크 여유가 `5.3 * S + 30 GiB` 이상이다(`S`는 다섯 DB 주파일 크기의 합이다).
- 실패: 정지하지 않는다.

각 서비스 단위가 읽는 환경도 확인한다. `show -p Environment` 출력에는 값이 들어 있다. 키 이름만 읽고 값은 `<RUN>`이나 보고에 옮기지 않는다.

```bash
systemctl --user show -p EnvironmentFiles -p Environment -p WorkingDirectory <UNIT_FRONT> <UNIT_PROXY> <UNIT_GW> <UNIT_API> <UNIT_REINGEST>
grep -c '^LOCAL_DATA_DIR=' <ENVFILE>
grep -c '^SINGLE_SQLITE_FILE=' <ENVFILE>
```

- 합격: `LOCAL_DATA_DIR` 키가 있는 환경 파일이 한 곳이다(그 키 출현이 1이다). `SINGLE_SQLITE_FILE` 키 출현이 0이다. 모든 단위에서 `LOCAL_DATA_DIR`의 출처가 같은 파일 하나다.
- 이유: 설정은 `LOCALCRAB_ENV_FILE`이 가리키는 파일(기본 `~/.openclaw/localcrab-kure.env`)을 읽고 현재 디렉터리의 `.env`가 그 위를 덮는다. 실제 환경변수가 둘 다 이긴다. 단위가 쓰는 파일과 `WorkingDirectory`의 `.env`가 다르면 5단계 설정이 일부 단위에만 적용된다.

#### 1단계 정지

```bash
systemctl --user stop <TIMER_REINGEST>
systemctl --user disable <TIMER_REINGEST>
systemctl --user is-active <UNIT_REINGEST>
```

- 합격: `<UNIT_REINGEST>`가 inactive나 failed다. activating이나 active이면 끝날 때까지 같은 명령을 반복한다. 대기 상한은 최근 실행에서 읽은 시작부터 완료까지 최대 시간의 두 배다. 상한을 넘으면 서비스를 stop하지 않고 타이머를 `enable --now`로 되살리고 다시 정한다. 재인입이 끝난 뒤 미완 표식(전체 재적재 대기)이 없는지 로그를 읽는다. 표식이 있으면 스위치하지 않는다.

```bash
systemctl --user stop <UNIT_FRONT>
systemctl --user stop <UNIT_PROXY>
systemctl --user stop <UNIT_GW>
systemctl --user stop <UNIT_API>
systemctl --user stop <UNIT_REINGEST>
mkdir -p <RT>/<UNIT_FRONT>.d <RT>/<UNIT_PROXY>.d <RT>/<UNIT_GW>.d <RT>/<UNIT_API>.d
printf '[Service]\nExecStartPre=/bin/false\n' > <RT>/<UNIT_FRONT>.d/block.conf
printf '[Service]\nExecStartPre=/bin/false\n' > <RT>/<UNIT_PROXY>.d/block.conf
printf '[Service]\nExecStartPre=/bin/false\n' > <RT>/<UNIT_GW>.d/block.conf
printf '[Service]\nExecStartPre=/bin/false\n' > <RT>/<UNIT_API>.d/block.conf
systemctl --user daemon-reload
systemctl --user show -p DropInPaths <UNIT_FRONT> <UNIT_PROXY> <UNIT_GW> <UNIT_API>
systemctl --user show -p ActiveState -p MainPID <UNIT_FRONT> <UNIT_PROXY> <UNIT_GW> <UNIT_API> <UNIT_REINGEST>
fuser -v <ROOT>/opencrab.db <ROOT>/graph.db <ROOT>/doc_store.db <ROOT>/vectors.db <ROOT>/billing.db
date -u +%Y-%m-%dT%H:%M:%S+00:00
```

- 합격: 넷 모두 `DropInPaths`에 `block.conf`가 보인다(시작 차단). 단위 파일이 `~/.config/systemd/user`에 있으면 `mask --runtime`은 효과가 없다. 그 디렉터리가 런타임 디렉터리보다 우선해 `LoadState`가 `loaded`로 남고, `mask`는 `already exists`로 거부된다. 그래서 런타임 drop-in으로 `start`를 실패시킨다. 이 차단 파일은 런타임 디렉터리에 있어 재부팅하면 사라진다. 모든 단위가 inactive이고 `MainPID`가 0이다. `fuser` 표준 출력에 PID가 없다(`-wal`과 `-shm`이 있으면 같은 방식으로 본다). 마지막 줄의 값이 `<T0>`이다.
- 실패: 1b로 가지 않는다. 남은 단위와 점유 프로세스를 처리하고 이 단계를 다시 한다. 코드나 데이터를 바꾸기 전이므로 서비스를 다시 켜고 끝낼 수 있다.

#### 1b 코드 배포 (정지 뒤)

```bash
git -C <REPO> fetch origin
git -C <REPO> merge --ff-only <대상 커밋>
git -C <REPO> rev-parse HEAD
<PY> -c "from opencrab.config import Settings; print('single_sqlite_file' in Settings.model_fields)"
<PY> -m pip install -e <REPO> --no-deps
<PY> -m pip show opencrab
```

- 합격: `HEAD`가 대상 커밋이다. 마지막 `python -c` 출력이 `True`다. `pip show`의 `Version`이 `pyproject.toml`의 버전과 같다(editable 설치는 재설치 전까지 설치 메타데이터의 버전이 옛 값으로 남는다. `opencrab --version`과 `pip show`는 그 메타데이터를 읽고 `opencrab.__version__`은 소스를 읽는다. `docs/migrations.md` 10절의 확인 명령 참고).
- 실패: 갈래 A.

저장소 밖에서 이 저장소를 부르는 운영 스크립트(예: 재인입 스크립트)가 있으면 이 시점에 같은 방식으로 배포하고 대상 커밋의 해시로 대조한다. 실패하면 갈래 A.

#### 2단계 정지 상태 증거 백업

병합 원본 세트는 DB 다섯만 든 디렉터리를 다시 백업해 만든다. 첫 백업은 정지한 운영 디렉터리를, 둘째 백업은 푼 사본을 대상으로 한다. 두 백업 모두 `SINGLE_SQLITE_FILE`이 설정되지 않은 환경에서 돌린다. 설정이 있으면 단일 root용 백업이 선택된다. 백업 도구를 `write.lock`을 쥔 채 호출하지 않는다.

```bash
fuser -v <ROOT>/opencrab.db <ROOT>/graph.db <ROOT>/doc_store.db <ROOT>/vectors.db <ROOT>/billing.db
mkdir <DATA_PARENT>/stage-a
<HEAVY> <PY> -m opencrab.stores.backup --data-dir <ROOT> --to <DATA_PARENT>/stage-a --exclude chroma --exclude docs --exclude graph.kuzu
<PY> -m opencrab.stores.backup --verify <DATA_PARENT>/stage-a/<SETA>
```

- 합격: 점유 프로세스 0. `--verify`가 rc 0이다. `--exclude` 대상 중 없는 항목은 세트가 `skipped`로 기록한다.
- 실패: 갈래 E(백업은 데이터를 바꾸지 않는다).

```bash
<HEAVY> <PY> <REPO>/scripts/unpack_backup_set.py <DATA_PARENT>/stage-a/<SETA> <DATA_PARENT>/stage-unpack
<HEAVY> <PY> -m opencrab.stores.backup --data-dir <DATA_PARENT>/stage-unpack --to <DATA_PARENT>
<PY> -m opencrab.stores.backup --verify <DATA_PARENT>/<SETC>
ls <DATA_PARENT>/<SETC>
```

- 합격: 푸는 도구의 마지막 줄이 `ALL OK`이고 rc 0이다. 푸는 도구는 이름이 정해진 DB 다섯만 복사하고 삭제 저널 같은 하위 경로 항목과 심볼릭 링크 항목은 건너뛰어 이름을 출력한다. 대상 디렉터리가 이미 있으면 거부한다. 둘째 백업의 `--verify`가 rc 0이다. `<SETC>` 안에 `MANIFEST.json`과 DB 다섯만 있고 `<SETC>`는 `<DATA_PARENT>`의 직접 자식이다.
- 한계: 푸는 도구는 벡터 DB 파일 이름이 기본 `vectors.db`인 세트만 받는다. 다른 이름을 쓰는 세트는 rc 2로 끝난다.

```bash
bash -c 'set -o noclobber; : > <DATA_PARENT>/<SETC>.source.lifetime.lock'
stat -c '%F %s %h' <DATA_PARENT>/<SETC>.source.lifetime.lock
date -u +%Y-%m-%dT%H:%M:%S+00:00
<PY> <REPO>/scripts/make_publication_evidence.py <DATA_PARENT>/<SETC> --out <DATA_PARENT>/evidence.json --preparer-id <PREPARER> --frozen-from <T0> --packs-checked-at <T1> --frozen-until <T2>
stat -c '%d %n' <DATA_PARENT> <DATA_PARENT>/<SETC> <DATA_PARENT>/evidence.json
```

- 합격: `stat` 출력이 `regular empty file 0 1`이다. 둘째 `date` 출력이 `<T1>`이다. `<T2>`는 3단계 `mv` 직전으로 예상하는 UTC 시각을 몇 분 뒤로 잡는다. 증거 도구가 `evidence written`을 출력한다. 장치 번호가 셋 같다. 증거 파일은 root, `<OLD>`, `<SETC>` 안에 있지 않다.
- 증거 도구는 병합 도구의 규칙 함수로 만든 증거를 검사한다. 규칙을 어기면 파일을 만들지 않고 rc 2로 끝난다. `--out`이 세트 디렉터리 안이면 거부한다. 이 도구는 `writer_stopped`, `billing_same_snapshot`을 참으로, `packs_observation`을 `absent`로 고정한다. 1단계에서 정지를 실제로 확인한 사람이 하는 선언이다.

#### 3단계 경로 전환과 병합 게시

```bash
fuser -v <ROOT>/opencrab.db <ROOT>/graph.db <ROOT>/doc_store.db <ROOT>/vectors.db <ROOT>/billing.db
stat -c %d <ROOT> <DATA_PARENT>
ls -d <OLD>
mv -T <ROOT> <OLD>
ls -d <ROOT> <OLD>
mkdir <TMP>
```

- 합격: 점유 0. 장치 번호가 같다. `ls -d <OLD>`가 `No such file`이다. `mv` 뒤 `<ROOT>`는 없고 `<OLD>`가 있다. 하나라도 어긋나면 `mv`하지 않는다(갈래 E).

```bash
<HEAVY> <PY> <REPO>/scripts/merge_sqlite_stores.py --source-dir <DATA_PARENT>/<SETC> --publication-root <ROOT> --evidence-file <DATA_PARENT>/evidence.json
```

- 합격: `publication mode is not ready`와 rc 2다. 계획 모드의 정상 출력이다. 원본 세트 경로와 게시 root가 위 값과 같은지 읽는다.

```bash
env SQLITE_TMPDIR=<TMP> <HEAVY> <PY> <REPO>/scripts/merge_sqlite_stores.py --source-dir <DATA_PARENT>/<SETC> --publication-root <ROOT> --evidence-file <DATA_PARENT>/evidence.json --apply --verify --immutable-source > <RUN>/3-merge.out 2>&1
cat <RUN>/3-merge.out
ls -A <ROOT>
```

- 합격: rc 0이고 마지막 줄이 `publication ready`다. `--verify` 출력에 `{"verify": "counts", "mismatch": 0}` 줄과 `"vec_mismatch": 0`을 담은 `{"verify": "fts_vec", ...}` 줄이 모두 있다. `<ROOT>` 안 이름이 정확히 `billing.db`, `ledger.json`, `manifest.json`, `marker.json`, `single.db`, `single.db.lifetime.lock`이다. 경과 시간을 기록한다.
- 실패: rc가 0이 아니면 먼저 경과 시간, `MemoryError`, rc 128 초과(신호)를 보고 상한 종료인지 가른다. 메모리 부족 거부(`<HEAVY>` 래퍼가 내는 rc 75. 래퍼가 없으면 이 경우는 없다)는 병합을 시작하지 않은 것이므로 갈래 E다. 그 밖의 실패는 갈래 C.

#### 4단계 authorize

```bash
<HEAVY> <PY> <REPO>/scripts/merge_sqlite_stores.py authorize --root <ROOT> --run-kind production --verification-pass <PASS1> --copy-test-pass <PASS2>
<HEAVY> <PY> <REPO>/scripts/merge_sqlite_stores.py authorize --root <ROOT> --run-kind production --verification-pass <PASS1> --copy-test-pass <PASS2> --apply > <RUN>/4-authorize.out 2>&1
cat <RUN>/4-authorize.out
cat <ROOT>/ledger.json
cat <DATA_PARENT>/<ROOT 이름>.intent.json
```

- 합격: 첫 명령이 `authorize dry-run ok`를 출력한다. 둘째 명령의 `authorize applied` 줄에 찍힌 증거 해시 둘이 0단계에 적은 두 PASS 해시와 같다. ledger state가 `authorized`이고 intent state가 `ready`다(8.3).
- 실패: 갈래 C(8.5의 표).

#### 5단계 항목 이동과 설정

```bash
systemctl --user is-active <UNIT_REINGEST>
systemctl --user is-active <TIMER_REINGEST>
find <OLD>/packs -printf '%P %s\n' > <RUN>/5-before-packs.txt
find <OLD>/openclaw-conversations -printf '%P %s\n' > <RUN>/5-before-conversations.txt
find <OLD>/datasets -printf '%P %s\n' > <RUN>/5-before-datasets.txt
env -C <OLD>/packs find . -type f -exec sha256sum {} + > <RUN>/5-packs-sha-before.txt
stat -c %d <OLD> <ROOT>
ls -d <ROOT>/packs
mv -T <OLD>/packs <ROOT>/packs
find <ROOT>/packs -printf '%P %s\n' > <RUN>/5-after-packs.txt
diff <RUN>/5-before-packs.txt <RUN>/5-after-packs.txt
ls -d <ROOT>/openclaw-conversations
mv -T <OLD>/openclaw-conversations <ROOT>/openclaw-conversations
find <ROOT>/openclaw-conversations -printf '%P %s\n' > <RUN>/5-after-conversations.txt
diff <RUN>/5-before-conversations.txt <RUN>/5-after-conversations.txt
ls -d <ROOT>/datasets
mv -T <OLD>/datasets <ROOT>/datasets
find <ROOT>/datasets -printf '%P %s\n' > <RUN>/5-after-datasets.txt
diff <RUN>/5-before-datasets.txt <RUN>/5-after-datasets.txt
env -C <ROOT>/packs find . -type f -exec sha256sum {} + > <RUN>/5-packs-sha-after.txt
diff <RUN>/5-packs-sha-before.txt <RUN>/5-packs-sha-after.txt
```

- 합격: 서비스와 타이머가 inactive다. 장치 번호가 같고 세 `ls -d`가 `No such file`이다. 항목마다 이동 직후의 `diff`가 0줄이다(8.6 P3, P4). 마지막 sha `diff`도 0줄이다(`env -C`로 상대 경로 목록을 만들어 접두어가 같다. 줄 순서만 달라 `diff`가 나오면 두 파일을 `sort -o`(같은 파일을 입력과 출력으로)로 각각 정렬해 다시 비교한다). 없는 항목은 건너뛴다(그 항목은 이 운영 환경에 없다). 어느 `diff`든 어긋나면 다음 항목을 옮기지 않고 갈래 E다.
- 외부 스크립트가 기본 데이터 경로를 고정 문자열로 쓰는지 `grep -rn '<DATA 경로 문자열>' <외부 스크립트 디렉터리>`로 확인한다(8.6 P8).

```bash
tail -c 1 <ENVFILE>
printf 'SINGLE_SQLITE_FILE=<ROOT>/single.db\n' >> <ENVFILE>
grep -c '^SINGLE_SQLITE_FILE=' <ENVFILE>
ls -ld <ROOT>/openclaw-conversations/automation
```

- 합격: 첫 명령이 줄바꿈을 낸다(아니면 다음 줄 앞에 줄바꿈을 먼저 넣는다). 둘째 `grep`이 1이다. 재인입 단위의 로그 경로 부모가 있다(8.6 P6).
- `LOCAL_DATA_DIR`은 그대로 `<ROOT>`다. 설정 순서는 8.7을 따른다.

#### 6단계 읽기 검증 (단위는 멈춘 채, 임시 인스턴스만)

임시 인스턴스는 운영 단위와 같은 환경으로 띄운다. 운영 단위의 `Environment`와 `WorkingDirectory`를 `systemctl --user show -p Environment -p WorkingDirectory <단위>`로 읽는다. 출력에 값이 들어 있으므로 키 이름만 확인하고 값은 기록하지 않는다. 사본 시험에서 어긋난 곳은 세 군데였다.

- `STORAGE_MODE=local`이 빠지면 임시 인스턴스가 다른 모드로 뜬다.
- `PATH`가 다르면 GGUF 폴백 같은 선택 기능이 다르게 동작한다.
- 게이트웨이 형태의 임시 인스턴스는 `WorkingDirectory`를 홈 디렉터리로 두어 운영 단위와 같게 맞춘다. 저장소 디렉터리에서 띄우면 그 `.env`가 설정을 덮는다.

격리 시험(합성 데이터)에서는 `HOME`과 `LOCALCRAB_ENV_FILE`도 덮어써 운영 환경 파일을 읽지 않게 한다(0단계의 설정 규칙). 포트는 운영이 쓰지 않는 값을 쓴다.

```bash
systemd-run --user --unit=<TMP_GW> --collect -p EnvironmentFile=<ENVFILE> -p Environment=STORAGE_MODE=local -p Environment=PATH=<운영 단위와 같은 PATH> -p WorkingDirectory=<홈 디렉터리> <PY> -m opencrab.cli serve --transport http --host 127.0.0.1 --port <GW_PORT> --allow-query-token
<PY> <REPO>/scripts/check_read_endpoints.py <GW_PORT> mcp --token-env <토큰 변수> --expected <기준 팩 목록 파일>
systemctl --user stop <TMP_GW>
```

REST 앱 형태는 같은 환경으로 아래처럼 띄운다. `python -m apps.api.main`은 모든 인터페이스(0.0.0.0)에 바인드하므로 쓰지 않는다. `WorkingDirectory`는 운영 REST 단위의 값(저장소 디렉터리)과 같게 둔다.

```bash
systemd-run --user --unit=<TMP_API> --collect -p EnvironmentFile=<ENVFILE> -p Environment=STORAGE_MODE=local -p Environment=PATH=<운영 단위와 같은 PATH> -p WorkingDirectory=<REPO> <PY> -m uvicorn apps.api.main:app --host 127.0.0.1 --port <API_PORT>
<PY> <REPO>/scripts/check_read_endpoints.py <API_PORT> api --token-env <토큰 변수> --expected <기준 팩 목록 파일>
systemctl --user stop <TMP_API>
```

인증 프록시 형태는 운영 프록시 단위의 시작 명령을 `systemctl --user show -p ExecStart <UNIT_PROXY>`로 읽고(값에 비밀이 있으면 옮기지 않는다), 같은 명령을 `--host 127.0.0.1 --port <PROXY_PORT>`와 `<TMP_PROXY>` 이름으로 띄운다. 검사는 게이트웨이와 같은 `mcp` 형태로 `<PROXY_PORT>`에 돌린다.

- 합격: `RESULT: PASS`와 rc 0이다. `ontology_query`의 토큰 요청이 200이고 `isError`가 거짓이며 본문에 `error` 키가 없고 `results` 목록이 있다. 무토큰 요청이 401이다. `content_pack_list`가 200이고 `expected_minus_got`가 `[]`이다. `got_minus_expected`는 읽고 기록한다. 출력에 토큰이나 헤더 값이 없다.
- 기준 팩 목록 파일은 전환 전 운영 인스턴스에 같은 도구를 `--save-expected <기준 팩 목록 파일>`로 돌려 만든다(0단계에서 서비스가 도는 동안 한 읽기 질의). 이 파일은 팩 id 목록이므로 저장소에 올리지 않는다.
- 토큰은 환경변수로 넘기고 명령줄에 쓰지 않는다(`--client-config <클라이언트 설정 파일> --server <서버 이름>`도 쓸 수 있다).
- P5가 요구하는 나머지 두 검사는 사람이 한다. `opencrab packs list`(환경은 같은 `<ENVFILE>`)가 `<ROOT>/packs/`의 manifest 팩을 모두 보이는지 보고, `content_pack_list`의 query로 manifest 키워드에만 있는 단어를 검색해 그 팩이 돌아오는지 본다. 운영에 manifest 팩이 있을 때만 해당한다.
- 도구가 보내는 질의에 `ontology_query`와 `POST /api/query`가 있다. 이 둘은 팩 데이터를 바꾸지 않지만 billing 행과 질의 이벤트를 쓴다. 7단계의 롤백 지점 백업이 그 행을 포함하도록 6단계 임시 인스턴스를 먼저 멈춘다.

```bash
systemctl --user is-active <TMP_GW> <TMP_PROXY> <TMP_API>
```

- 합격: 임시 단위가 모두 inactive이거나 not found다.
- 실패: 쓰기 개방 전 갈래 B.

#### 7단계 롤백 지점 기록 (쓰기 개방 직전)

```bash
fuser -v <ROOT>/single.db <ROOT>/billing.db
mkdir -p <DATA_PARENT>/backups/rollback-point
stat -c %d <ROOT> <DATA_PARENT>/backups
<HEAVY> env SINGLE_SQLITE_FILE=<ROOT>/single.db LOCAL_DATA_DIR=<ROOT> <PY> -m opencrab.stores.backup --to <DATA_PARENT>/backups/rollback-point
env SINGLE_SQLITE_FILE=<ROOT>/single.db LOCAL_DATA_DIR=<ROOT> <PY> -m opencrab.stores.backup --verify <DATA_PARENT>/backups/rollback-point/<롤백 세트 이름>
cp -a <ROOT>/openclaw-conversations/automation <DATA_PARENT>/automation.rollback-copy
env -C <ROOT>/openclaw-conversations/automation find . -type f -exec sha256sum {} +
env -C <DATA_PARENT>/automation.rollback-copy find . -type f -exec sha256sum {} +
env -C <ROOT>/packs find . -type f -exec sha256sum {} + > <RUN>/7-packs-sha.txt
```

- 합격: 점유 0. 장치 번호가 같다. 백업과 `--verify`가 rc 0이고 세트가 `<ROOT>` 밖에 있으며 `single.db`, `billing.db`, `MANIFEST.json`만 든다. 두 sha256 목록이 경로 접두어를 빼고 같다. 롤백 세트 경로와 사본 경로를 `<RUN>`에 적는다. 롤백은 `packs`를 복원하지 않는다.
- 실패: 쓰기를 열지 않는다. 갈래 B.

#### 8단계 쓰기 개방

```bash
systemctl --user is-active <TMP_GW> <TMP_PROXY> <TMP_API>
fuser -v <ROOT>/single.db <ROOT>/billing.db
rm <RT>/<UNIT_FRONT>.d/block.conf
rm <RT>/<UNIT_PROXY>.d/block.conf
rm <RT>/<UNIT_GW>.d/block.conf
rm <RT>/<UNIT_API>.d/block.conf
rmdir <RT>/<UNIT_FRONT>.d <RT>/<UNIT_PROXY>.d <RT>/<UNIT_GW>.d <RT>/<UNIT_API>.d
systemctl --user daemon-reload
systemctl --user start <UNIT_GW>
systemctl --user start <UNIT_PROXY>
systemctl --user start <UNIT_API>
systemctl --user start <UNIT_FRONT>
systemctl --user show -p ActiveState -p MainPID -p NRestarts <UNIT_GW> <UNIT_PROXY> <UNIT_API> <UNIT_FRONT>
systemctl --user show -p MainPID --value <UNIT_GW>
grep -z -c '^SINGLE_SQLITE_FILE=' /proc/<게이트웨이 MainPID>/environ
```

- 합격: 임시 단위 inactive, 점유 0. 네 단위가 active이고 `MainPID`가 0이 아니며 `NRestarts`가 0이다. 단위마다 `grep -z -c`가 1이다(프록시와 REST 앱도 같은 명령을 각 `MainPID`로 돌린다. `-z`는 각 `MainPID`의 `/proc` environ 파일의 NUL 구분 항목을 한 줄씩 읽는다). 재인입 타이머는 아직 `enable`하지 않는다.
- 실패: 재인입 타이머가 disable 상태인지 확인하고 갈래 D를 시작할지 정한다.

#### 9단계 개방 직후 확인

```bash
ls -ld <ROOT>/openclaw-conversations/automation
<PY> <REPO>/scripts/check_read_endpoints.py <GW_PORT_운영> mcp --token-env <토큰 변수> --expected <기준 팩 목록 파일>
systemctl --user start <UNIT_REINGEST>
systemctl --user is-active <UNIT_REINGEST>
journalctl --user -u <UNIT_REINGEST> -n 80 --no-pager
```

- 합격: 읽기 검증이 `RESULT: PASS`다. 이 단계는 쓰기 도구를 호출하지 않는다(쓰기 경로는 사본 시험이 이미 확인했다). 재인입 단위는 운영 환경 고유이므로 아래 판정은 그 단위의 로그를 읽어 한다. 재인입이 끝나고 최종 상태가 성공이거나 새 메시지 없음이다. 이벤트 `graph_import_delta`와 `doc_nodes_sync`가 ok다.
- 실패: 거부 원인은 stderr의 JSON으로 가른다. 커서나 `packs` 위치 거부는 5단계 이동 누락(8.6 P1, P6)이고, 단일 root인데 설정값이 비었다는 거부는 0단계의 환경 파일 확인으로 돌아간다. 고치지 못하면 타이머를 enable하지 않고 갈래 D를 정한다.

```bash
systemctl --user enable --now <TIMER_REINGEST>
systemctl --user is-enabled <TIMER_REINGEST>
```

- 합격: `enabled`. 밀린 실행이 한 번 일어날 수 있다.
- 스위치 뒤 정리: `stage-a`, `stage-unpack`, `<SETC>`와 source lock, `evidence.json`, `<TMP>`, `automation.rollback-copy`, `<OLD>`는 보존 기간이 끝나기 전에 지우지 않는다. 지우기 전에 인용 검색과 재현 가능성을 확인하고 확인을 받는다.

#### 롤백 갈래

- 갈래 A, 정지 뒤 1b 실패: 데이터를 건드리지 않았다. 체크아웃을 0단계에 적은 커밋으로 `git reset --keep`하고 `rev-parse HEAD`로 확인한다. 이어서 `<PY> -m pip install -e <REPO> --no-deps`를 다시 돌리고 `pip show opencrab`의 `Version`이 되돌린 체크아웃의 `pyproject.toml` 버전과 같은지 확인한다(설치 메타데이터가 1b의 새 버전으로 남지 않게 한다). 저장소 밖 스크립트를 되돌린다. 1단계의 시작 차단 파일을 지우고(`rm <RT>/<단위>.d/block.conf`, `rmdir`, `daemon-reload`) 단위와 재인입 타이머를 켠다. `systemctl --user enable --now <TIMER_REINGEST>`.
- 갈래 B, 쓰기 개방 전 롤백: 임시 단위가 inactive인지 확인하고 1단계의 정지 확인을 다시 한다. 8.6 P7의 순서(이동 항목 되돌리기, 이름 맞바꾸기, intent와 잠금 파일 보존, 설정 줄 지우기)를 따른다. 설정 줄은 `sed -i '/^SINGLE_SQLITE_FILE=/d' <ENVFILE>`로 지우고 `grep -c`가 0인지 확인한다. 모든 `mv -T` 앞에서 장치 번호가 같고 대상 이름이 없음을 확인한다. 마지막에 시작 차단 파일 제거, 단위 시작, 타이머 enable, 환경의 `SINGLE_SQLITE_FILE` 출현 0을 확인한다.
- 갈래 C, 병합이나 authorize의 비정상 종료: 8.4(게시)와 8.5(authorize)를 따른다. 다시 돌릴지 다섯 파일로 돌아갈지는 사람이 정해 기록한다.
- 갈래 D, 쓰기 개방 뒤 롤백: 1단계의 정지를 그대로 한다(타이머 stop과 disable, 단위 stop, 시작 차단 파일). 합격 기준도 그대로 확인하되 `fuser` 대상은 `<ROOT>/single.db`, `<ROOT>/billing.db`와 각 `-wal`, `-shm`이다. 정지를 확인한 뒤 둘 중 하나로 간다. 개방 뒤의 MCP 쓰기는 두 경우 모두 롤백 지점 뒤 것이 사라진다. 대화 메시지는 커서를 되돌렸으므로 델타 적재가 다시 처리한다.
  - D1 단일 파일로 되돌린다: `<PY> -m opencrab.stores.single_restore <DATA_PARENT>/backups/rollback-point/<롤백 세트 이름>`(dry-run)을 읽고 `--apply`를 붙여 돌린다. 그다음 `automation` 사본을 교체한다. 교체는 이동 두 번이다. 각 이동 앞에서 두 경로의 장치 번호가 같고 대상 이름이 없음을 확인한다(8.6 P2). 먼저 `<ROOT>/openclaw-conversations/automation`을 `<DATA_PARENT>/automation.saved-<UTC 시각>`으로 `mv -T`하고, 이어서 `<DATA_PARENT>/automation.rollback-copy`를 원래 자리로 `mv -T`한다(8.6 P7의 "사본을 그 자리로 옮긴다"). 사본은 이 이동으로 소비되며, 7단계의 sha256 대조가 사본이 롤백 지점 때의 `automation`과 같음을 이미 확인했다. 마지막으로 시작 차단 파일 제거와 `daemon-reload`, 8단계 순서의 단위 시작, 9단계의 읽기 검증, 타이머 enable을 한다. 합격: `--apply`가 rc 0과 `결속 검증을 통과했다`를 내고, 복원한 `automation`의 `cursor_state.json`이 사본과 같으며, 읽기 검증이 `RESULT: PASS`이고, 각 `MainPID`의 환경에 `SINGLE_SQLITE_FILE`이 1건 있다.
  - D2 다섯 파일로 돌아간다: 갈래 B의 순서(이동 항목을 `<OLD>`로 되돌리기, 이름 맞바꾸기, intent와 잠금 파일 보존, 설정 줄 지우기)를 따르되 `automation` 교체를 이름 맞바꾸기 앞에 한다(8.6 P7). 항목을 `<OLD>`로 되돌린 직후 `<OLD>/openclaw-conversations/automation`을 D1과 같은 이동 두 번으로 사본으로 바꾼다. 갈래 B가 끝나는 단위 시작과 타이머 enable은 이 교체 뒤에 한다. 합격: 읽기 검증이 `RESULT: PASS`이고 각 `MainPID`의 환경에 `SINGLE_SQLITE_FILE`이 없다.
- 갈래 E, 단계 중단 공통: 다음 단계로 가지 않는다. 원인을 고쳐 그 단계를 다시 할지 갈래 A나 B로 갈지 정해 결정과 다시 판정할 시각을 기록한다.

#### 합성 데이터 전 과정 확인

이 표는 합성 다섯 파일 데이터 디렉터리 하나로 0단계부터 9단계까지 단계마다 실행해 확인했다. 확인한 점은 다음과 같다.

- 합성 데이터는 `opencrab init`과 임시 인스턴스의 `pack_create` 호출로 만들었다. 임베딩 서버는 결정적 벡터를 돌려주는 시험용 대역이다.
- 서비스는 임시 이름의 사용자 유닛과 운영이 쓰지 않는 루프백 포트로 띄웠다. 운영 데이터와 운영 단위는 읽지도 쓰지도 않았다.
- 모든 명령은 격리한 `HOME`, `LOCALCRAB_ENV_FILE`, `LOCAL_DATA_DIR` 아래에서 돌렸다. 병합과 백업은 무거운 실행 래퍼로 하나씩 돌렸다.
- 갈래 D1은 쓰기 개방 뒤 쓰기를 만든 상태에서 한 번 돌려 합격 기준을 확인했다. D2는 개방 뒤 상태에서 갈래 B의 순서와 `automation` 교체를 함께 돌려 확인했다. 개방 전만의 갈래 B와 갈래 A는 따로 돌리지 않았다.
- 시작 차단 파일은 합성 확인에서 `<UNIT_FRONT>`, `<UNIT_PROXY>`, `<UNIT_GW>` 셋으로 확인했다. `<UNIT_API>` 추가는 같은 명령의 반복이다.
- REST 임시 인스턴스는 `uvicorn ... --host 127.0.0.1` 형태로 읽기 검증 `RESULT: PASS`를 확인했다. 인증 프록시 형태와 6단계의 `packs list`, manifest 키워드 검색은 합성 환경에 대응물이 없어 돌리지 못했다(프록시는 단위 대역이고 합성 팩에 manifest가 없다).
- 재인입 서비스는 성공 메시지만 내는 대역으로 확인했다. 9단계의 재인입 이벤트(`graph_import_delta`, `doc_nodes_sync`) 판정은 이 확인으로 검증하지 못했다.
- 단계별 확인 결과의 요약은 PR 본문에 있다.

## 9. 단일 모드 벡터 질의 경로: 한 스냅숏과 ANN 캐시

단일 파일 모드의 벡터 질의가 다섯 파일 모드보다 늦었다. 이 절은 원인과 수리 설계, 불변식, 시험을 적는다. 구현은 `opencrab/stores/sqlite_vec_store.py`와 `opencrab/pack/load.py`에 있다.

### 9.1 문제

운영 DB 사본에서 같은 질의 목록을 같은 heavy-run 틀(주소 공간 2 GiB, nice 19, ionice 3)로 옛 코드(다섯 파일, A)와 단일 모드(C)에서 재었다. 절대값은 제약 아래의 값이므로 비율로만 읽는다. C는 A보다 분명히 늦었고 공간 필터가 붙은 질의의 차이가 가장 컸다. 비율은 측정 시점과 사본마다 달라 이 문서에 적지 않는다. 같은 절차는 9.5의 1번 게이트가 정한다(A와 C를 교대로 3회 이상 재고 호출별 웜 중앙값과 벡터 다리 단독 중앙값을 비교한다).

원인은 `SqliteVecStore`의 공유 모드 조건 둘이다. `git log -S'self._shared is None and pack_values' -- opencrab/stores/sqlite_vec_store.py`(조건 1)와 `git log -S'self._shared is None and self._ann' -- opencrab/stores/sqlite_vec_store.py`(조건 2)가 같은 두 커밋을 낸다. 조건이 처음 들어간 커밋과 이 변경의 커밋이다.

1. `query()`의 대규모 범위 fallback이 `self._shared is None`일 때만 켜진다. 읽을 수 있는 팩이 `_PACK_KNN_MAX`(32)를 넘어도 공유 모드는 팩별 KNN을 직렬로 돈다.
2. `_select_query_mode()`의 binary ANN 경로도 `self._shared is None`일 때만 켜진다. 운영 설정은 `VECTOR_ANN=binary`이므로 단일 모드는 ANN 캐시를 쓰지 못했다.

두 조건의 목적은 한 질의가 한 스냅숏을 보게 하는 것이다. 지연 비용을 검토한 기록은 없다.

### 9.2 불변식

- I1. 단일 모드에서 쓰기 구간 밖의 질의는 모든 벡터 읽기를 한 읽기 스냅숏 안에서 한다. 질의 도중 다른 연결이 commit해도 그 질의가 읽는 행은 변하지 않는다. I1은 읽는 행에만 걸린다. 후보를 만드는 ANN 캐시는 질의 스냅숏과 다른 시점일 수 있고, 그 경우 후보에서 빠진 행은 결과에 나오지 않을 수 있다.
- I2. 쓰기 구간 밖 읽기 경로는 다섯 파일 모드와 같은 검색 알고리즘(대규모 범위 fallback, binary ANN, 같은 캐시 신선도 규칙)을 쓴다.
- I3. 이미 열린 트랜잭션 안(쓰기 구간 `unit_of_work`이나 합류 트랜잭션)의 질의는 종전과 같다. ANN 캐시를 읽지 않고 만들지 않으며 미커밋 벡터를 본다.
- I4. 접근 제어는 최종 후필터가 정한다. 후필터는 스냅숏 안에서 읽어 온 행의 메타데이터에 적용한다. ANN 캐시는 후보를 만들 뿐 접근 판정을 하지 않는다.

### 9.3 설계

A. 스냅숏을 먼저 정하고 fallback을 그 결과에 묶는다. `query()`가 질의 임베딩을 계산한 뒤 대규모 범위 fallback 판정 앞에서 `snapshot_opened`를 정한다(공유 모드이고 연결이 트랜잭션 밖이면 참). 조건 `self._shared is None`을 `(self._shared is None or snapshot_opened)`로 바꾼다. 팩이 32개를 넘으면 `pack_values`를 비우고 `pack_only`를 거짓으로 두며 `pack_scope`를 세운다. 이 두 값의 결합은 기존 주석이 요구하는 그대로다.

B. 쓰기 구간 밖 공유 모드 읽기를 한 스냅숏으로 감싼다. `snapshot_opened`가 참이면 `query()`가 `try` 안에서 `_open_snapshot()`(`BEGIN`)을 열고 `finally`에서 `rollback()`한다. 열기를 `try` 밖에 두면 `BEGIN` 뒤 예외가 롤백을 건너뛰어 그 스레드 연결에 트랜잭션이 남고 다음 `unit_of_work`가 거부된다. 스냅숏 안에서 캐시 신선도 판정과 빌드, 후보 후필터, 정밀 재계산이 돈다. 최종 `predicate`는 메모리에 가져온 행에 적용한다. `_open_snapshot()`은 시험이 스냅숏 없는 음성 대조를 만들 이음매이다.

C. `_select_query_mode()`가 `allow_ann`을 받는다. `query()`는 `allow_ann = (self._shared is None) or snapshot_opened`로 넘긴다. 다섯 파일 모드는 그대로이다.

D. ANN 캐시 신선도는 다섯 파일 모드의 규칙을 바꾸지 않는다. `_get_ann_cache`는 `_ann_cache_fresh`를 잠금 밖과 안에서 부르고, 그 시점에 캐시가 `None`이 아니면 매 호출이 연결의 `data_version`을 기록한다. 캐시가 `None`이면 잠금 밖과 안의 신선도 호출을 모두 건너뛰고 `data_version`도 기록하지 않는다. 첫 호출에서 `data_version`이 바뀌었으면 거짓이다. 둘째 호출은 `data_version`이 기록값과 같으므로 벡터 표 `_rowids`의 `max(rowid)`가 캐시의 값과 같은지만 본다. 단일 모드에서는 질의가 스냅숏 안에서 `data_version`을 읽으므로 두 호출의 값이 같다. 그래서 그래프, 문서, 레지스트리 쓰기가 `data_version`을 올려도 벡터 `max(rowid)`가 그대로면 캐시를 유지한다. 벡터 삽입은 `max(rowid)`를 올려 다음 질의가 재빌드한다.
- 알려진 한계(기존 동작): 가장 큰 rowid가 아닌 행만 지운 삭제는 `max(rowid)`가 그대로라 캐시가 낡은 채 남을 수 있다. 낡은 id는 정밀 재계산이 건너뛰므로(I4) 영향은 재현율이다. 다섯 파일 모드에도 있으며 #706이 추적한다.
- 재빌드는 스냅숏 안의 연결로 한다. 스냅숏 밖에서 빌드하면 읽는 사이 쓰기가 끼어 id와 벡터가 어긋날 수 있다.
- 오래된 스냅숏 가드는 두지 않는다. 최상위 rowid를 지운 뒤 모든 질의가 정확 KNN으로 내려가는 회귀를 만든다.
- 이 변경이 단일 모드에 ANN 캐시를 처음 연다. 콜드 첫 질의는 다섯 파일 모드와 같은 빌드 비용을 치른다.

벡터 표를 바꾸는 쓰기 경로는 `grep -rnE '(DELETE FROM|INSERT INTO|INSERT OR [A-Z]+ INTO|UPDATE) *(\{[^}]*(table|collection|_table)[^}]*\}|vectors_kure)' opencrab scripts apps`로 찾았다. 삽입은 새 rowid를 만든다. 삭제는 삭제만 있는 변경이라 위 한계에 든다. 제자리 UPDATE는 메타데이터만 바꾸는 `_vec_meta_update`와 `scripts/backfill_space.py`뿐이며 반례가 아니다. 캐시가 `ids`, `bits`, `q8`, `scale`만 담고, 반환 행은 정밀 재계산이 스냅숏 안 점조회로 읽기 때문이다. 임베딩을 제자리로 바꾸는 문장이 생기면 이 가정을 다시 본다.

E. 캐시 무효화를 commit 뒤로 옮긴다. 쓰기 메서드 다섯 곳이 `with self._tx()` 다음 줄에서 캐시를 비우던 자리는 단일 모드에서 commit 앞이었다. 다른 스레드가 그 사이에 재빌드하면 commit 전 스냅숏으로 캐시가 다시 서고, 삭제만 있는 commit이면 낡은 캐시가 남는다.
- 헬퍼 `_invalidate_ann_after_write()`가 그 자리를 대신한다. 쓰기 구간 안이면 `after_commit` 콜백으로 commit 뒤에 `_drop_ann_cache()`(`_ann_cache = None`과 `_ann_epoch += 1`, `_ann_epoch_lock` 안)를 실행한다. 롤백과 commit 실패에서는 콜백이 호출되지 않아 캐시가 그대로이다. 쓰기 구간 밖이면 즉시 실행한다. 콜백이 한 번 더 돌면 `epoch`가 한 번 더 오르지만 효과(캐시 비움)는 같다. 그래서 콜백 중복 방지 플래그를 두지 않는다.
- 빌드는 오래 걸려 commit을 가로지를 수 있다. 가로지른 빌드가 commit 전 스냅숏의 캐시를 콜백 뒤에 설치하면 낡은 캐시가 되살아난다. `_get_ann_cache`는 첫 줄에서 `epoch`를 읽는다. 스냅숏은 그 뒤 첫 DB 읽기(`_ann_cache_fresh`의 `PRAGMA data_version` 또는 `_build_ann_cache`)에서 고정된다. 설치는 `_ann_epoch_lock` 안에서 `epoch`를 비교하고 같을 때만 한다. 비교와 설치가 한 임계 구역이라 비교 직후 콜백이 끼는 틈이 없다. `query()`의 `BEGIN`과 `_get_ann_cache` 첫 줄 사이에는 DB 읽기가 없다는 것이 이 순서의 전제이다.
- 대기 스레드는 `_ann_cache_lock`을 얻은 직후 (1) 신선한 캐시가 있으면 쓰고, (2) 없고 `epoch`가 달라졌으면 빌드하지 않고 `None`을 돌려주어 정확 KNN으로 가며, (3) 그러지 않으면 빌드한다. (2)가 없으면 대기 스레드마다 설치가 거부될 빌드를 차례로 돈다. commit 뒤 새 연결이 연결별 `data_version` 때문에 각자 재빌드하는 중복은 다섯 파일 모드에서 상속한 동작이며 #707이 추적한다.
- 락 순서는 `_ann_cache_lock`에서 `_ann_epoch_lock`으로 한 방향이다. 콜백은 `shared.lock`을 놓은 뒤 `_ann_epoch_lock`만 잡는다. 질의 경로는 `shared.lock`을 잡지 않는다.
- `opencrab/pack/load.py`의 직접 DELETE 두 곳이 같은 헬퍼를 부른다. 단일 파일 삭제 적용은 기존 commit 앞 무효화를 헬퍼 호출로 바꾸고, 지울 벡터가 있을 때만 부른다. sql 분기 팩 삭제는 commit 블록과 삭제 수 확인 뒤에 부른다. 헬퍼가 없는 시험 대역은 `getattr` 가드로 건너뛴다. 이 호출은 다섯 파일 모드에서도 돌아 같은 프로세스의 캐시를 비운다.

F. 다섯 파일 모드는 `_select_query_mode`의 서명, 무효화 헬퍼, 설치 때의 `epoch` 비교, 팩 삭제 뒤 헬퍼 호출, 대기 규칙을 빼고 바뀌지 않는다. 대기 규칙은 `_get_ann_cache`가 두 모드에 공통이므로 다섯 파일 모드에도 걸린다. 쓰기가 곧바로 `epoch`를 올리므로 빌드 잠금을 기다리던 질의가 그 사이 쓰기를 만나면 빌드하지 않고 정확 KNN 경로로 간다. 종전 코드는 그 질의도 빌드했다. 범위 질의에서 이 경로는 전역 후보(4096)를 최종 후필터로 거르는 다섯 파일 모드의 기존 근사 경로이고, 접근 판정은 최종 후필터가 하므로 접근 결과는 같다. 다섯 파일 모드의 공간 필터가 벡터를 놓치는 문제는 #705가 맡는다.

### 9.4 기대 결과와 승인된 차이

- 쓰기 구간 밖 단일 모드 벡터 다리가 다섯 파일 모드와 같은 알고리즘이 된다. 사본 시험의 질의 목록에서 단일 모드와 옛 코드가 달랐던 결과가 같아져야 한다.
- 운영 설정은 `ann=binary`이다. `ann=""`인 단일 모드는 팩 33개 이상 범위에서 팩별 정확 KNN 대신 전역 KNN(k=4096)과 후필터를 쓴다. 다섯 파일 모드와 같은 알고리즘이며 재현율이 종전과 다를 수 있다.
- 공간 필터 질의 하나는 종전 단일 모드보다 적게 돌려준다. 원인은 `_select_query_mode`가 잔여 predicate가 있어도 `pack_scope`가 있으면 ANN을 허용하는 조건이며 다섯 파일 모드와 같다. 이 결함은 #705가 다룬다.
- `schema_pack_list`는 이 수정과 무관하다. 코드 검사가 코드 체크아웃의 `types/<t>.yaml`을 보므로 이름공간 구성 차이이다.

### 9.5 게이트

구현을 막는 게이트는 다섯 개이다.
1. 지연: 질의 목록의 중앙값과 p95와 호출별 웜 중앙값과 콜드 빌드 시간이 A의 1.2배 이하이다.
2. 사본 시험의 질의 목록 결과가 `schema_pack_list`를 뺀 전부 같다.
3. 전체 시험 묶음을 시험 id별로 대사한다.
4. 스냅숏 불변식 시험과 그 음성 대조가 통과한다.
5. 같은 프로세스의 쓰기 스레드가 있을 때 질의가 잘못된 결과를 내지 않는다.

그 밖의 항목(WAL 크기, 메모리, 재빌드 횟수 상한, 변이 검증 등)은 측정할 수 있으면 기록하고 막지 않는다. 지연 측정은 같은 heavy-run 틀에서 A와 C를 교대로 3회 이상 잰다.

### 9.6 시험

- `tests/test_single_file_vector_ann.py`: 팩 삭제 도우미의 commit 뒤 무효화(단일 파일은 구간 안 유지와 구간 뒤 비움, 다섯 파일은 즉시), 대규모 범위 질의가 범위 밖 팩과 공간을 돌려주지 않음(ann 꺼짐과 binary, 공간 필터 있음과 없음)과 항등 후보 필터 아래에서도 같음, 트랜잭션 안 대규모 범위의 팩별 질의, 빈 표 질의와 전부 지운 뒤 질의, 스냅숏 고정과 음성 대조(`_open_snapshot`을 끈 쪽은 삭제를 본다), 예외 뒤 열린 트랜잭션 없음(스냅숏 안의 예외와 `BEGIN` 뒤 열기 실패 둘), 대규모 범위의 전역 KNN 한 번, 쓰기 구간 밖 ANN 사용과 안 불사용, 단일 모드와 다섯 파일 모드의 동등성, 낡은 후보 제거(I4), 접근 제어 predicate, 상위 rowid 삭제와 외부 삽입의 재빌드와 비벡터 commit의 비재빌드, 제자리 메타 갱신, commit 뒤 무효화와 롤백, 빌드가 commit을 가로지름, 웜 경로의 `epoch` 순서, 비교와 설치의 같은 임계 구역, 대기 스레드, `load.py` 헬퍼 가드.
- `tests/test_single_file_store.py`: 옛 시험 둘(`..._queries_each_partition_in_one_snapshot`, `..._never_uses_ann_cache`)은 위 파일의 대체 시험으로 옮겼다. 단일 파일 삭제 적용 시험이 commit 뒤 무효화(`epoch` 상승)와 실패 때의 무변화를 단언한다. 새 시험 하나(`stores_ann` 픽스처)는 무효화 호출 뒤에 실패를 주입해 롤백 때 캐시와 `epoch`가 그대로임을 단언한다.
- 변이 확인: 폐기용 checkout에서 변이를 하나씩 적용해 시험 묶음(위 두 파일과 `tests/test_pack_load.py`)을 돌렸다. 치환 전 문자열이 대상 파일에 정확히 한 번 나오는지 먼저 확인했다. 사망한 변이는 스냅숏 끔, `allow_ann` 항상 거짓, 트랜잭션 안 ANN 허용, commit 앞 무효화, 설치 때 `epoch` 비교 제거, `epoch` 증가 제거, `epoch`를 잠금 안에서 읽기, 대기 스레드의 `epoch` 재확인 제거, 비교와 설치 분리, `max(rowid)` 비교 제거, 단일 파일 삭제 자리의 헬퍼 호출 제거와 즉시 비움, sql 분기 팩 삭제의 헬퍼 호출 제거, 헬퍼의 즉시 비움, `pack_scope`가 있을 때 최종 후필터 건너뛰기, 트랜잭션 안의 대규모 범위 fallback, 빈 캐시 가드 제거다. 항등 `_filter_candidates_by_pack`은 생존한다. 최종 후필터가 접근을 정하므로 그 후보 필터는 접근 결함이 아니라 회수율과 속도의 장치이기 때문이다. 삽입 계열과 `reset_collection`의 무효화를 지운 변이는 `max(rowid)`가 변화를 잡아 결과가 같으므로 실행하지 않았고 시험을 더하지 않았다.

### 9.7 위험과 미해결

- 가장 큰 rowid가 아닌 행만 지운 삭제가 캐시를 낡게 두는 한계는 다른 프로세스의 쓰기와 오프라인 스크립트에서 남는다. 운영의 2-instance 서빙에서는 한 인스턴스의 팩 삭제가 다른 인스턴스의 캐시를 낡게 둔다. 영향은 재현율이며 다음 벡터 삽입이나 재시작이 바로잡는다. #706이 추적한다.
- commit 뒤 대기 스레드의 재빌드 중복은 다섯 파일 모드에서 상속했다. #707이 추적한다.
- 쓰기 간격이 빌드 시간보다 짧으면 빌드한 질의는 설치를 거부당하고, 그 사이 기다리던 질의는 빌드 없이 전역 정확 KNN 경로로 간다. 같은 부하의 A와 비교해 잰다.
- 콜드 첫 질의, 빌드 중 읽기 스냅숏이 붙잡는 WAL, 캐시 메모리는 측정으로 확인한다. 빌드를 스냅숏 밖에서 하는 대안은 일관된 빌드를 보장하지 못해 택하지 않는다.
- 되돌리기: 코드 변경이므로 `git revert`로 종전 동작에 돌아간다. 데이터 형식은 바꾸지 않는다.
