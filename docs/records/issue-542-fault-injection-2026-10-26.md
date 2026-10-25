# #542 노드 BM25 DB 색인 장애 주입 기록 (2026-10-26)

이 기록은 2026-10-26에 관측한 값이다. 갱신하지 않는다. 설계, 판정 기준, 재현 명령은 `docs/issue-542-fault-injection.md`에 있다. 다음 측정은 새 날짜의 기록으로 남긴다.

## 조건

- 코드: 이 기록을 더한 브랜치. 제품 코드는 base(`origin/main`, #541 포함)와 같다. 하네스는 `scripts/search_faults.py`다.
- 코퍼스: `scripts/search_corpus.py generate --preset S1 --seed 542`. 노드 20150, 질의 42. 적재는 `scripts/search_baseline.py load`로 했고 적재 완료 표시가 코퍼스의 기대 상태와 일치했다.
- 실행: transient systemd 서비스(`CPUQuota=100%`, `Nice=19`, `IOSchedulingClass=best-effort`, `IOSchedulingPriority=7`, `LimitAS=2G`, `MALLOC_ARENA_MAX=2`, `PYTHONDONTWRITEBYTECODE=1`). 실행 중 주 PID의 `/proc/<pid>/limits`에서 주소 공간 상한 2147483648바이트, cgroup `cpu.max`에서 `100000 100000`을 확인했다. 보고서 `environment`에도 같은 값이 남았다. Python 3.13.5, SQLite 3.46.1.
- 장치 등급: 보고서 `environment`의 `MemTotal`은 8453685248바이트(약 7.9GiB), CPU 수는 4, 기계 종류는 aarch64다. 한 번에 한 실행만 돌렸다.
- 결과: 보고서 `verdict: COMPLETE`. 시나리오 21개 모두 주입을 확인했다(`not_injected` 0, `unavailable` 0). 하네스 프로세스의 표본 peak RSS는 약 461MiB였다.

## 기준 상태

기준 사본은 `ready`, pending 0, `verify` 일치였다. 질의 42개 모두 DB 색인 결과가 전량 오라클(`BM25Index.build`)과 항목, 점수, 순서까지 같았다. 기준 분류는 BM25 pass 41과 partial_silent 1, 결합 pass 40과 miss 1과 partial_silent 1이다. partial_silent는 `top-k-pressure-1`(#550)이다. 노드 색인 바이트(`dbstat`)는 15183872였다.

## 장애별 판정

| 장애 | 판정 | 시나리오 값 |
|---|---|---|
| 타임아웃 | explicit_error | T1 explicit_error, T2 explicit_error, T3 explicit_error, T4 verified_fallback |
| 취소 | explicit_error | C1 explicit_error, C2 verified_fallback, C3 explicit_error |
| 디스크 부족 | explicit_error | D1a, D1b, D2, D3 모두 explicit_error |
| 색인 오류 | silent_partial | E1 explicit_error, E2a silent_partial, E2b explicit_error, E3 silent_partial |
| 중단 후 재개 | explicit_error | K1, K2, K3 모두 explicit_error |
| 재시작 수렴 | verified_fallback | R1 verified_fallback, R2 verified_fallback, R3 verified_fallback |

모든 시나리오에서 복구 판정(설계 3.5절)이 성립했다. 복구는 `opencrab packs bm25-index` 명령(`backfill`, `verify`, `drop --yes`, `status`)과 저장소 재시작만으로 했다. 복구 뒤 상태 `ready`, pending 0, `verify` CLI 종료 코드 0, 질의 42개의 DB 결과와 오라클 일치, 분류가 기준과 같음을 모두 확인했다. 색인 오류의 `silent_partial` 두 건도 장애 동안의 질의 표면 판정이며, 복구 자체는 성립했다.

## 시나리오별 관측

### 타임아웃

- T1: 정답 키 45개를 pending으로 둔 채 다른 연결이 SQLite 쓰기 잠금을 쥐었다. 질의 통과 동안 pending이 45개 그대로였고 질의 42개 가운데 39개의 BM25 결과에 pending 경고가 붙었다. 나머지 셋을 포함해 3.2절 위반은 0이다. BM25 호출 지연은 p50 50.9ms, p95 53.6ms, 최댓값 54.7ms였다(레그 요구치 500ms). 내부 잠금을 다른 스레드가 쥔 경우도 pending이 그대로였고 위반이 0이었다(p95 2.9ms). 두 잠금 단계 모두 잠금을 쥔 채, 풀기 전에 pending 45개를 뺀 `verify`가 일치했다. 잠금을 푼 뒤 다음 검색이 pending을 모두 소진했고 질의별 분류와 `wrong_pack`이 기준과 같았다.
- T2: 잠금 중 새 노드 쓰기가 `OperationalError('database is locked')`로 실패했다. 그 키는 원본에도 pending에도 없었고 직후 `verify`가 일치했다.
- T3: 저장소를 연 뒤 잠금을 쥐자 backfill 단계가 종료 코드 1과 `{"error": "database is locked", "processed": 0}`로 끝났다. 커서, 상태, 색인 문서 수가 그대로였다. 같은 시간의 질의 통과는 메모리 경로였고 위반이 0이다.
- T4: 잠금을 `busy_timeout`보다 길게 쥔 동안 연 저장소 인스턴스는 사용 가능(`available`)이지만 색인이 `unsupported`였다. 잠금을 푼 뒤에도 그 인스턴스는 `unsupported`로 남았다. 그 인스턴스의 질의 42개는 모두 메모리 경로였고 경고 없는 결과가 오라클과 같았다. 그 인스턴스로 쓴 새 노드는 요청한 본문으로 원본에 있었고 키는 pending에 남았다. 자식이 끝난 직후 pending 1개를 뺀 `verify`가 일치했다. 새로 연 프로세스가 pending을 소진했다.

### 취소

- C1: pending이 있는 DB 경로 BM25 호출 한 번의 진행 처리기 호출 수는 192(100 opcode 간격)였다. 시점 60개에서 한 번씩 취소했고 60개 모두 coverage 경고가 붙은 결과였다. 시점마다 열린 트랜잭션이 없었고, 내부 잠금을 바로 얻었고, 키 단위 불변식이 성립했다. 끝의 전량 `verify`도 일치했다. 처리기를 지운 뒤의 질의 통과는 분류와 `wrong_pack`이 기준과 같았다.
- C2: backfill 상태(메모리 경로)의 cold 구축을 포함한 호출의 처리기 호출 수는 19746이었다. 시점 10개 가운데 9개는 오라클과 같은 결과, 1개는 coverage 경고가 붙은 결과였다. 메모리 창 읽기가 취소되면 창을 Python에서 다시 읽는 기존 처리(경고 로그 `BM25 budgeted window query failed`)가 결과를 복구했다. 시점마다 열린 트랜잭션이 없었고, 내부 잠금을 바로 얻었고, backfill 불변식이 성립했다. 처리기를 지운 뒤의 질의 통과는 분류와 `wrong_pack`이 기준과 같았다.
- C3: 색인 25%에서 보낸 `SIGINT`는 종료 코드 1과 표준 오류 `Aborted!`로 끝났다. 50%에서 보낸 `SIGTERM`은 신호 종료(-15)였다. `--max-seconds 0.2`는 종료 코드 0과 상태 `backfill`(처리 603)을 출력했고 DB 상태도 `backfill`이었다. 세 번 모두 부분 단계가 없었다(색인 키 집합이 커서 이하 키 집합과 같음). 세 번 모두 멈춘 직후 재시작한 서비스로 질의 통과를 했고, 메모리 경로에서 위반이 0이었다.

### 디스크 부족

- D1a: `RLIMIT_FSIZE` 5061290바이트를 건 backfill은 2010개를 처리한 뒤 종료 코드 1과 `{"error": "disk I/O error"}`로 끝났다. 부분 단계는 없었다.
- D1b: 크기 60829696바이트의 tmpfs(비특권 사용자 네임스페이스) 안의 backfill은 1809개를 처리한 뒤 종료 코드 1과 `{"error": "database or disk is full"}`로 끝났다. tmpfs 사본과 되돌려 복사한 사본의 상태, 커서, 색인 문서 수, pending이 같았다. 부분 단계는 없었다.
- D2: `RLIMIT_FSIZE` 아래 쓰기는 2개가 성공하고 4개가 `OperationalError('disk I/O error')`로 실패했다. 성공한 키는 요청한 본문으로 원본에 있었고 표지 토큰으로 찾혔다. 실패한 키는 원본에 없었다. 단계마다 검색 전에 `verify`가 일치했다. `max_page_count`를 건 쓰기는 `OperationalError('database or disk is full')`로 실패했고 그 키는 원본과 pending에 없었다.
- D3: `RLIMIT_FSIZE` 아래의 검색은 소진 때마다 `node BM25 search-time drain skipped: disk I/O error`를 debug 로그로 남기고 pending 정답을 결과에서 빼며 경고를 붙였다. 경고 없는 결과는 오라클과 같았다. 끝난 뒤 pending 45개를 뺀 `verify`가 일치했다. 한도 없이 한 질의 통과는 분류와 `wrong_pack`이 기준과 같았다.

### 색인 오류

- E1: 키 순서 가운데 키(`evidence`, `l1-006926`)의 재색인 실패를 주입한 backfill은 10050개를 처리한 뒤 종료 코드 1과 그 키를 담은 오류로 끝났다. 커서는 그 키 앞에 멈췄고 상태는 `backfill`이며 부분 단계가 없었다. 그 키를 건너뛰고 `ready`가 되는 일은 없었다.
- E2a: 아래 "제품 결함 후보" 1번.
- E2b: 시드로 고른 잎 블록 8개의 바이트를 뒤집었다. `integrity-check`와 `instance` 어휘 표 읽기가 `database disk image is malformed`를 냈다. 질의 42개의 결과는 손상 블록에 닿지 않아 모두 오라클과 같았다(3.2절 위반 0). `verify` CLI는 같은 SQLite 오류의 traceback과 종료 코드 1로 끝났다.
- E3: E3ii(금지 문서의 색인 `pack_id`를 요청 팩으로 바꿈)는 후보 SQL에 그 문서가 들어온 것을 확인했지만 검색의 현재 행 재검사가 걸러 누출이 0이었다. E3i는 아래 "제품 결함 후보" 2번.

### 중단 후 재개

- K1: 색인 25%, 50%, 75%에서 보낸 `SIGKILL`은 세 번 모두 신호 종료(-9)였다. 매번 상태 `backfill`, 커서 전진, 부분 단계 없음이 성립했다. 끝까지 다시 돌린 backfill로 복구했다.
- K2: 쓰기 42번째(본문 수정)의 트랜잭션 안, `doc_nodes` 쓰기 뒤 커밋 전에 멈춘 자식에 `SIGKILL`을 보냈다(-9). `ack`한 41개의 변경이 모두 원본에 있었고, 끊긴 수정은 커밋되지 않은 이전 상태였다. pending은 0이고 `verify`가 일치했다.
- K3: 대형 팩 하나의 8000개를 pending에 넣고 소진 중 `SIGKILL`(-9)을 보냈다. pending은 7968개가 남았고, pending을 뺀 `verify`가 일치했다. pending이 남은 질의 통과는 pending 정답이 걸린 질의에 경고를 붙였고 위반이 0이다. `backfill` CLI로 소진해 복구했다.

### 재시작 수렴

- R1: backfill 동안 서비스 역할의 `HybridQuery`는 메모리 경로였다. backfill 실행 중 쓰기 60건을 했고, 그 가운데 직접 SQL 수정 30건은 같은 트랜잭션에서 읽은 상태가 `backfill`이고 커서가 있었다. backfill 자식은 시작 뒤 11.96초에 종료 코드 0으로 끝났다. 쓴 키마다 마지막으로 요청한 본문이 원본에 있었다. backfill이 끝나자 같은 인스턴스가 재시작 없이 DB 경로로 바뀌었고 질의 통과의 위반이 0이었다. 새 프로세스의 BM25 결과가 그 인스턴스의 결과와 같았다.
- R2: `OPENCRAB_BM25_MAX_NODE_CHARS=4096`으로 연 프로세스가 색인을 `backfill`로 초기화했다(`meta`의 값 4096). 그동안 서비스의 질의 통과는 메모리 경로에서 위반이 0이다. 원래 설정으로 다시 열고 backfill로 복구했다.
- R3: `doc_nodes`를 같은 스키마로 다시 만들며 어느 질의도 참조하지 않는 문서 하나를 뺐다. 다시 열자 트리거 셋이 다시 생기고 행 수 점검이 색인을 `backfill`로 되돌렸다. 그동안 질의 통과는 메모리 경로에서 위반이 0이다.

## 제품 결함 후보

### 1. FTS5 구조 레코드 손상이 빈 색인으로 읽혀 조용한 부분 결과가 된다 (E2a)

- 재현 조건: `ready` 색인의 `doc_nodes_bm25_fts_data`에서 구조 레코드(`id=10`) 블록의 바이트 네 곳을 뒤집는다(`scripts/search_faults.py run ... --scenarios E2a`).
- 관측 결과: FTS5 `integrity-check`는 통과했다. `instance` 어휘 표 전량 읽기는 오류 없이 0행을 돌려줬고 stats의 `total_len`은 5070647이었다. 다시 연 저장소는 `ready`였다. 검색은 DB 경로로 돌며 정답을 놓친 결과를 coverage 경고 없이 돌려줬다. BM25 레그 `pass`가 `partial_silent`로 바뀐 질의가 다수였고, 결합 결과도 경고 없이 정답을 놓친 질의가 있었다. `verify` CLI는 `mismatched_terms` 20150과 종료 코드 1로 손상을 찾았다. `drop --yes`, `status`, `backfill`로 복구됐다.
- 기대 결과: 설계 10절 판정대로 명시 오류나 검증된 fallback이어야 한다. 예를 들어 검색이 색인의 용어 수와 stats의 불일치를 보면 `BM25 coverage unavailable` 경고를 내거나 기존 경로로 돌아가야 한다.

### 2. 색인 표에서 지운 항목이 경고 없이 빠진다 (E3i)

- 재현 조건: `ready` 색인에서 노드 형태 정답 하나의 `doc_nodes_bm25_docs` 행과 FTS 행을 지우고 stats는 그대로 둔다(`--scenarios E3`).
- 관측 결과: 그 정답의 질의(`exact-recent-0`)가 BM25 `pass`에서 `partial_silent`로 바뀌었고 경고가 없었다. 다시 연 저장소는 stats의 문서 수가 원본 행 수와 같으므로 초기화하지 않았다. `verify` CLI는 `missing` 1, `mismatched_meta` 1, `stats_ok` false와 종료 코드 1로 찾았다. 같은 명령으로 복구됐다.
- 기대 결과: #541 4.4절은 감지 범위를 `doc_nodes` 쓰기(트리거)로 정하고 색인 표의 직접 변경은 `verify`로 찾는다고 둔다. 이 관측은 그 경계가 검색 표면에서는 조용한 부분 결과로 드러난다는 사실이다. 설계 10절 판정 기준으로는 위반이다.

## 판정 밖 관측

- T4: 열기 때 잠금 대기가 `busy_timeout`을 넘은 저장소 인스턴스는 수명 동안 `unsupported`이며 잠금이 풀려도 DB 경로로 돌아오지 않는다. S1 기본 설정에서는 메모리 창이 전량을 담아 결과가 오라클과 같았다. 창이 전량을 담지 못하는 규모에서는 coverage 경고가 붙는 기존 경로가 되며, 재시작해야 DB 경로로 돌아온다.
- 디스크 부족의 `RLIMIT_FSIZE` 수단은 SQLite `disk I/O error`를, tmpfs와 `max_page_count` 수단은 `database or disk is full`을 냈다. 제품 처리 경로는 같았다.
