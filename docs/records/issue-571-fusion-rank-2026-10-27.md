# #571 결합 결과의 어휘 레그 상위 2 보장 측정 기록 (2026-10-27)

이 기록은 2026-10-27에 관측한 값이다. 갱신하지 않는다. 설계와 판정 근거는 `docs/issue-571-fusion-rank.md`에 있다.

## 1. 조건

- 코드: 측정 시점의 `origin/main`(#556과 #567 포함). probe는 저장소 파일을 바꾸지 않았다.
- 코퍼스: `scripts/search_corpus.py generate --preset <S1|S2|S3> --seed 542`. 질의는 셋 모두 48이다. 적재는 같은 코드의 `scripts/search_baseline.py load`이고 셋 모두 `COMPLETE`였다(노드 20150, 61100, 120000).
- 실행: 생성, 적재, probe를 transient systemd 서비스(`CPUQuota=100%`, `Nice=19`, `IOSchedulingClass=best-effort`, `IOSchedulingPriority=7`, `LimitAS=2G`, `RuntimeMaxSec`, `MALLOC_ARENA_MAX=2`, `PYTHONDONTWRITEBYTECODE=1`, `TMPDIR`은 디스크 경로)로 하나씩 돌렸다. 각 서비스는 python을 실행하기 직전에 같은 PID의 셸에서 주소 공간 상한, cgroup `cpu.max`, nice, ionice, 환경 변수를 파일로 남겼다. 모든 서비스에서 주소 공간 상한 2GiB, `cpu.max` `100000 100000`, nice 19, best-effort 7, `MALLOC_ARENA_MAX=2`였다.
- 장치 등급: 8GB RAM, 4코어 aarch64.

## 2. 착수 전 probe: 후보 비교

- 방법: 적재 디렉터리의 새 사본에서 하네스와 같은 `resolve_packs`와 `HybridQuery.query`를 질의 48개마다 rerank 경로와 평면 병합 경로로 불렀다. 프로세스 안에서 레그 메서드와 `_reserve_leg_tops`를 감싸 레그 목록(점수 포함)과 접기 뒤 목록(#556 단계 앞)을 기록했다. 후보 규칙은 기록한 목록에 오프라인으로 적용했고, 분류는 하네스의 `judge_bundle`로 냈다. RRF 상수 후보는 `opencrab.ontology.reranker`의 `_RRF_K`, `_ALPHA`를 프로세스 안에서 바꿔 다시 불렀다.
- 후보:
  - `cur`: 지금 규칙(#556). 어휘 레그 1위를 보장하고 vector 1위를 경고한다.
  - `place2`: 어휘 레그 상위 2를 보장한다. 순서는 BM25 1위, FTS 1위, BM25 2위, FTS 2위다. 보장 상한은 창의 절반(내림)이다.
  - `place3`: 같은 규칙으로 상위 3.
  - `warn2`: 1위만 보장하고, 어휘 레그 2위가 창 밖이면 경고만 단다.
  - `rel2_0.5`: `place2`에서 2위의 점수가 그 레그 1위 점수의 절반 이상일 때만 보장한다.
  - RRF `K:ALPHA`가 `20:0.7`, `60:0.5`, `10:0.7`인 rerank(지금 값은 `60:0.7`). 보장 규칙은 `cur`.
- 표기: 분류는 결합 분류 수다. `nofusion`은 `partial_silent` 가운데 `fusion:` 경고도 없는 질의다. "창 변경"은 반환 창의 id 목록이 `cur`와 다른 질의 수다.

### 2.1 `limit` 10 (하네스의 값)

| 코퍼스와 설정 | 경로 | 후보 | 결합 분류 | `fusion:` 경고 질의 | 창 변경 |
|---|---|---|---|---|---|
| S1 기본 | rerank | cur | pass 46, miss 1, partial_silent 1 | 5 | - |
| S1 기본 | rerank | place2 | pass 47, miss 1 | 5 | 6 |
| S1 기본 | rerank | place3 | pass 47, miss 1 | 5 | 6 |
| S1 기본 | rerank | warn2 | pass 46, miss 1, partial_silent 1 | 7 | 0 |
| S1 기본 | rerank | rel2_0.5 | pass 47, miss 1 | 5 | 4 |
| S1 기본 | rerank `20:0.7` | cur | pass 46, miss 1, partial_silent 1 | 5 | 12 |
| S1 기본 | rerank `60:0.5` | cur | pass 46, miss 1, partial_silent 1 | 5 | 4 |
| S1 기본 | rerank `10:0.7` | cur | pass 47, miss 1 | 1 | 14 |
| S1 기본 | 평면 | cur | pass 46, miss 1, partial_silent 1(nofusion) | 0 | - |
| S1 기본 | 평면 | place2 | pass 47, miss 1 | 0 | 13 |
| S1 기본 | 평면 | place3 | pass 47, miss 1 | 5 | 13 |
| S1 기본 | 평면 | warn2 | pass 46, miss 1, partial_silent 1 | 13 | 0 |
| S1 기본 | 평면 | rel2_0.5 | pass 47, miss 1 | 0 | 8 |
| S2 기본 | rerank | cur | pass 47, miss 1 | 7 | - |
| S2 기본 | rerank | place2 | pass 47, miss 1 | 7 | 6 |
| S2 기본 | rerank | place3 | pass 47, miss 1 | 8 | 8 |
| S2 기본 | rerank | warn2 | pass 47, miss 1 | 9 | 0 |
| S2 기본 | rerank | rel2_0.5 | pass 47, miss 1 | 7 | 3 |
| S2 기본 | rerank `20:0.7` | cur | pass 47, miss 1 | 2 | 13 |
| S2 기본 | rerank `60:0.5` | cur | pass 47, miss 1 | 7 | 2 |
| S2 기본 | rerank `10:0.7` | cur | pass 47, miss 1 | 2 | 14 |
| S2 기본 | 평면 | cur | pass 47, miss 1 | 0 | - |
| S2 기본 | 평면 | place2 | pass 47, miss 1 | 0 | 9 |
| S2 기본 | 평면 | place3 | pass 47, miss 1 | 2 | 11 |
| S2 기본 | 평면 | warn2 | pass 47, miss 1 | 9 | 0 |
| S2 기본 | 평면 | rel2_0.5 | pass 47, miss 1 | 0 | 4 |
| S3 기본 | rerank | cur | pass 46, miss 1, partial_silent 1(nofusion) | 4 | - |
| S3 기본 | rerank | place2 | pass 47, miss 1 | 4 | 8 |
| S3 기본 | rerank | place3 | pass 47, miss 1 | 7 | 9 |
| S3 기본 | rerank | warn2 | pass 46, miss 1, partial_silent 1 | 9 | 0 |
| S3 기본 | rerank | rel2_0.5 | pass 47, miss 1 | 4 | 7 |
| S3 기본 | rerank `20:0.7` | cur | pass 47, miss 1 | 3 | 11 |
| S3 기본 | rerank `60:0.5` | cur | pass 46, miss 1, partial_silent 1(nofusion) | 4 | 4 |
| S3 기본 | rerank `10:0.7` | cur | pass 47, miss 1 | 3 | 12 |
| S3 기본 | 평면 | cur | pass 45, miss 1, partial_silent 2(nofusion) | 0 | - |
| S3 기본 | 평면 | place2 | pass 47, miss 1 | 0 | 10 |
| S3 기본 | 평면 | place3 | pass 47, miss 1 | 2 | 13 |
| S3 기본 | 평면 | warn2 | pass 45, miss 1, partial_silent 2 | 10 | 0 |
| S3 기본 | 평면 | rel2_0.5 | pass 47, miss 1 | 0 | 6 |

- 축소 상한 설정(행 상한 = 노드의 25%, 올림)은 세 규모 모두 위 기본 설정 행과 분류, 경고 수, 창 변경 수가 같았다.
- 결합 `miss` 하나는 모든 행에서 `long-tail-1`이다(#542 기준선과 같은 기존 한계).
- rerank의 `partial_silent`는 `common-heavy-1`이다. S1에서는 다른 레그의 1위가 창 밖이라 `fusion:` 경고가 따로 붙어 있었고, S3에서는 경고가 없었다. S3 평면 병합의 둘은 `common-heavy-1`과 `whole-scope-0`이다. S1 평면 병합의 하나는 `common-heavy-1`이다.
- `common-heavy-1`의 놓친 정답은 S1에서 BM25 2위(접기 뒤 목록에서 rerank 18위, 평면 42위), S3에서 BM25 2위(rerank 13위, 평면 98위)였다. FTS와 vector 목록에는 없었다. 이 순위는 `limit` 10의 프로필에서 낸 값이다. S3 측정 기록 4.1절의 결합 순위 24는 `limit` 60으로 다시 부른 값이므로 다르다.
- `place2`의 창 변경에서 창을 떠난 결과 가운데 정답은 없었다(모든 코퍼스, 설정, 경로). 창에 들어온 결과는 정답, 대조군, 정답도 대조군도 아닌 결과였다. 질의별 목록은 3절에 있다.

### 2.2 작은 창 (`limit` 5와 3, 기본 설정)

같은 방법으로 모든 질의의 `limit`을 5와 3으로 바꿔 불렀다. 분류는 하네스 판정에 그 `limit`을 넣어 냈다. 하네스 자체는 `limit` 10만 쓴다.

| 코퍼스와 `limit` | 경로 | cur 분류 | cur 경고 | place2 분류 | place2 경고 |
|---|---|---|---|---|---|
| S1, 5 | rerank | pass 46, miss 1, partial_silent 1 | 7 | pass 47, miss 1 | 9 |
| S1, 5 | 평면 | pass 46, miss 1, partial_silent 1(nofusion) | 0 | pass 47, miss 1 | 9 |
| S3, 5 | rerank | pass 46, miss 1, partial_silent 1 | 7 | pass 46, miss 1, partial_silent 1 | 9 |
| S3, 5 | 평면 | pass 44, miss 1, partial_silent 3(nofusion) | 0 | pass 46, miss 1, partial_silent 1 | 6 |
| S1, 3 | rerank | pass 46, miss 1, partial_silent 1 | 14 | 같음 | 14 |
| S1, 3 | 평면 | pass 45, miss 1, partial_silent 2(nofusion) | 4 | pass 45, miss 1, partial_silent 2 | 15 |
| S3, 3 | rerank | pass 46, miss 1, partial_silent 1 | 11 | 같음 | 12 |
| S3, 3 | 평면 | pass 43, miss 1, partial_silent 4(3은 nofusion) | 4 | pass 43, miss 1, partial_silent 4 | 16 |

- `place2`에서 `nofusion`은 모든 행에서 0이다. `cur`는 평면 병합에서 경고 없는 누락이 남았다.
- `limit` 3은 보장 상한이 1이므로 `place2`도 창 구성이 `cur`와 같다. 경고만 는다.

### 2.3 어휘 레그 점수 비율 (S1 기본, 2위 점수 / 1위 점수)

- `common-heavy-1`: BM25 0.92(정답 2위). 3위 이하는 0.11.
- `top-k-pressure-1`: BM25 2위 이하 0.82에서 0.85(대조군). `top-k-pressure-0`, `-2`는 0.58, 0.51.
- `korean-variant-*`: BM25 2위 0.25에서 0.27(대조군).
- `common-only-*`: BM25와 FTS 상위 다섯이 0.97에서 1.00.

## 3. `place2`의 질의별 창 변경 (`limit` 10, rerank)

- S1(두 설정 같음): `common-heavy-1`(정답이 들어오고 정답 아닌 결과가 나감), `common-heavy-0`, `common-heavy-2`, `common-only-0`, `common-only-1`, `common-only-2`(들어온 결과와 나간 결과 모두 정답도 대조군도 아님).
- S2(두 설정 같음): `common-heavy-0`, `common-heavy-1`, `common-heavy-2`, `common-only-0`, `common-only-1`, `common-only-2`. 모두 정답도 대조군도 아닌 결과끼리 바뀌었다.
- S3(두 설정 같음): `common-heavy-1`(정답이 들어옴), `top-k-pressure-0`, `-1`, `-2`(대조군끼리 바뀜), `common-heavy-0`, `common-only-0`, `-1`, `-2`(정답도 대조군도 아닌 결과끼리).

평면 병합의 질의별 변경은 7절에 적는다.

## 4. 구현 뒤 재현 시험과 역변이

- RED: 구현 전 코드(설계 커밋)에서 `tests/test_query_lexical_reservation.py`에 더한 시험 일곱 개를 돌렸다. 여섯 개가 기대한 이유로 실패했다(rerank 결과에 BM25 2위 정답 없음, 평면 병합 끝 칸이 2위 정답 아님, 창 4칸에서 경고 수 0, 창 5칸에서 경고 없음, FTS 2위가 창 밖, 접힌 구성원 2위의 대표가 창 밖). 창 8칸 시험은 구현 전에도 통과한다. 창이 결과 전체를 담는 불변 확인이기 때문이다.
- 역변이: 구현 커밋의 폐기용 detached checkout에서 변이 셋을 하나씩 넣고 같은 시험 파일(시험 19개)을 돌렸다. 각 실행은 그 checkout의 `opencrab`을 import했다.
  - 깊이를 1로 되돌림: 새 시험 여섯 개 실패.
  - 순서를 BM25 1위, BM25 2위, FTS 1위, FTS 2위로 바꿈: 창 4칸 시험과 창 5칸 시험 실패.
  - FTS 2위를 넣지 않음: FTS 2위 시험 실패.
  - 변이 전과 복구 뒤의 대조군은 19개 모두 통과했다.

## 5. 구현 뒤 전체 스위트 대사

- 코드: base는 측정 시점의 `origin/main`(#567까지)의 detached worktree와 그 자신의 venv다. head는 구현 커밋이다. 측정 뒤 `origin/main`에 #542 S3 측정 기록(문서 하나)이 들어왔고 이 브랜치를 그 위로 옮겼다. 코드와 시험 파일은 바뀌지 않았으므로 측정을 다시 하지 않았다.
- 실행: `docs/gate-recipes.md` 6절의 재현 명령을 transient 서비스 안에서 base, head 순으로 하나씩 돌렸다. basetemp와 `TMPDIR`은 같은 디스크 경로다. 실행 중 pytest 프로세스에서 주소 공간 상한 2GiB, cgroup `cpu.max` `100000 100000`, 환경의 `MALLOC_ARENA_MAX=2`, `PYTEST_ADDOPTS=`(빈 값), `PY_COLORS=0`, `OPENCRAB_SMOKE_BIN_DIR`(각 checkout의 venv)를 확인했다. `OPENCRAB_PG_TEST_URL`은 두 실행 모두 두지 않았다. 공유 PostgreSQL을 쓰지 않기 위해서이며, 두 실행이 같은 PG 시험을 똑같이 건너뛴다.
- 결과: 두 요약 모두 `VERDICT:COMPLETE`. base는 tests 8903, skipped 508, 실패 6이다. head는 tests 8910, skipped 508, 실패 6이다. 실패 id 집합은 같다(`tests/test_issue80_sql_graph.py`의 PG와 Neo4j 런타임 시험 여섯 개, #556 기록 4절과 같은 필수 환경 변수 누락). 상태 대조에서 바뀐 항목은 새 시험 일곱 개 추가(모두 pass)뿐이다. pytest가 보고한 소요는 base 1842.9초, head 1820.9초였다.
- CI 기본 환경: 사용자 이름을 별도로 두고 `TMPDIR` 없이 기본 basetemp(시스템 임시 디렉터리)로 `test_query_lexical_reservation.py`, `test_query_characterization.py`, `test_query_duplicate_fold.py`, `test_query_retrieval.py`, `test_search_baseline.py`, `test_search_faults.py`를 돌려 114개 모두 통과했다. #542 하네스 S0 시험과 장애 주입 하네스 S0 시험이 여기에 든다. 실행 뒤 그 사용자의 임시 디렉터리를 지웠다.
- 하네스 `selftest`는 `SELFTEST: PASS`였다. 하네스 파일은 이 변경이 바꾸지 않았다.

## 6. 구현 뒤 #542 하네스 대조 (base와 head)

- 입력: 1절의 적재 한 벌(규모마다 하나)을 base와 head가 함께 썼다. `run`은 적재 디렉터리를 실행 디렉터리로 복사한 뒤 사본에서만 측정한다(#542 설계 8.2절). 적재는 base 코드로 했고, 이 변경은 적재 경로를 바꾸지 않는다.
- 실행: S1, S2, S3 각각 기본 설정과 축소 상한 설정에서 base, head 순으로 하나씩 돌렸다. 세 규모 모두 두 설정을 전량 돌렸다(줄이지 않았다). 열두 보고서 모두 `verdict: COMPLETE`, `bm25_index`는 `db`, 보고서의 주소 공간 상한은 2GiB, `cgroup_cpu_max`는 `100000 100000`이다. 서비스 소요는 S1 약 1분 30초, S2 약 2분 30초, S3 약 4분 50초였다.

| 규모와 설정 | base 결합 | head 결합 | `compare` |
|---|---|---|---|
| S1 기본 | pass 46, miss 1, partial_silent 1 | pass 47, miss 1 | CLEAN |
| S1 축소 상한 | pass 46, miss 1, partial_silent 1 | pass 47, miss 1 | CLEAN |
| S2 기본 | pass 47, miss 1 | pass 47, miss 1 | CLEAN |
| S2 축소 상한 | pass 47, miss 1 | pass 47, miss 1 | CLEAN |
| S3 기본 | pass 46, miss 1, partial_silent 1 | pass 47, miss 1 | REGRESSION 1(지연, 아래) |
| S3 축소 상한 | pass 46, miss 1, partial_silent 1 | pass 47, miss 1 | CLEAN |

- 레그 분류는 모든 보고서에서 BM25, FTS, vector 모두 pass 48이다.
- 질의별 대조: 레그와 결합의 분류, 누락, 정답 순위, 대조군 수, 자동 선택 판정이 바뀐 질의는 S1과 S3의 `common-heavy-1` 결합 하나다(`partial_silent`에서 `pass`). S2는 바뀐 질의가 없다. 결합 `miss` 하나는 모든 보고서에서 `long-tail-1`이다.
- 반복 결함 표지: 누출, 중복, 범위 오류, 경고 누락, 동시 쓰기 누출과 중복은 모든 보고서에서 0이다. 조용한 부분 결과 표지는 base의 S1과 S3에서 5(결합, 다섯 반복), head에서 모두 0이다.
- 요구치: base의 S1과 S3에서 미충족이던 `quality_partial_silent`가 head에서 충족됐다. `quality_combined_non_pass`는 `long-tail-1` 때문에 양쪽 모두 미충족이다.
- `fusion:` 경고가 켜진 질의(결합, 마지막 반복)는 모든 규모와 설정에서 base와 head가 같다. S1은 `common-heavy-0`, `common-heavy-1`, `common-heavy-2`, `common-only-1`, `top-k-pressure-2`. S2는 `common-heavy-0`, `common-heavy-1`, `common-only-0`, `common-only-1`, `top-k-pressure-0`, `-1`, `-2`. S3는 `common-heavy-2`, `common-only-1`, `common-only-2`, `top-k-pressure-2`.

### 6.1 S3 기본 설정의 지연 회귀 판정

- `compare`가 S3 기본 설정에서 `latency_whole_scope_p95_ms`(상한 2000ms)를 새 미충족으로 냈다. base 1995.9ms, head 2011.5ms였다. 같은 S3 축소 상한은 base 1969.8ms, head 1985.3ms로 둘 다 충족이었다.
- 같은 적재에서 S3 기본 설정을 head, base 순으로 한 번 더 돌렸다. head 1988.9ms(충족), base 2029.4ms(미충족)였고 `compare`는 CLEAN이었다. 두 번째 쌍의 결합 분류는 첫 쌍과 같았다(base pass 46, miss 1, partial_silent 1, head pass 47, miss 1).
- #542 S3 측정 기록(`docs/records/issue-542-s3-2026-10-27.md` 8절)도 같은 코드의 이 항목이 2065.1ms, 2000.1ms, 2004.3ms로 상한 근처에서 갈렸다고 적었다. 하네스의 p95는 표본 30개의 둘째로 큰 값이다.
- 이 변경은 Stage 7에서 보장 후보 목록을 넷까지 만들 뿐이고 레그 호출과 결합 계산을 바꾸지 않는다. 그래서 이 미충족은 상한 근처 항목의 실행 간 변동으로 판정한다. 이 판정은 위 두 쌍의 관측에 근거한다.
- 다른 지연 요구치의 충족 여부는 모든 규모와 설정에서 base와 head가 같았다. S3의 동시 쓰기 중 전체 범위 p95와 쓰기 지연 p95는 양쪽 모두 미충족이다(#565).

## 7. 구현 뒤 id 대조와 `fusion:` 경고 목록

- 방법: base checkout과 head checkout이 각각 같은 적재의 새 사본에서 질의 48개의 자동 선택, BM25, FTS, vector 레그, rerank 결합, 평면 병합의 id 목록과 결합 `warnings`를 냈다(2절의 probe 스크립트, RRF 변형 없이). 각 프로세스가 자기 checkout의 `opencrab`을 import했음을 출력으로 확인했다. 세 규모, 두 설정 모두 돌렸다.
- 자동 선택과 세 레그의 id 목록은 모든 규모와 설정에서 질의 48개 모두 같다. `fusion:`이 아닌 경고도 같다.
- `fusion:` 경고가 켜진 질의는 모든 규모, 설정, 경로에서 base와 head가 같다. rerank는 6절의 목록과 같고 평면 병합은 base와 head 모두 0개다.
- 반환 창에서 정답의 위치가 내려가거나 정답이 창을 떠난 질의는 없다.
- 두 설정의 결과는 규모마다 같다. 창이 바뀐 질의와 판정은 다음과 같다. 판정 규칙은 설계 8절이다.

| 규모 | 경로 | 질의 | 판정 |
|---|---|---|---|
| S1 | rerank | `common-heavy-1` | 개선. BM25 2위 정답이 창 밖에서 10위로 옴. 나간 결과는 정답도 대조군도 아님. |
| S1 | rerank | `common-heavy-2` | 중립. 정답 10위에서 9위. 정답 아닌 결과끼리 바뀜. |
| S1 | rerank | `common-heavy-0`, `common-only-0`, `-1`, `-2` | 중립. 정답 아닌 결과끼리 바뀜. 정답 위치 같음. |
| S1 | 평면 | `common-heavy-1` | 개선. BM25 2위 정답이 창 밖에서 9위로 옴. |
| S1 | 평면 | `common-heavy-2` | 중립. 정답 9위에서 7위. |
| S1 | 평면 | `korean-variant-0`, `-1`, `-2`, `top-k-pressure-2` | 중립(따로 적음). 대조군이 정답도 대조군도 아닌 결과를 밀어냄. 정답 위치 같음. |
| S1 | 평면 | `top-k-pressure-0`, `-1` | 중립. 대조군끼리 바뀜. |
| S1 | 평면 | `auto-title-0`, `common-heavy-0`, `common-only-0`, `-1`, `-2` | 중립. 정답 아닌 결과끼리 바뀜. `auto-title-0`과 `common-only`는 정답이 없는 질의다. |
| S2 | rerank | `common-heavy-0`, `-1`, `-2`, `common-only-0`, `-1`, `-2` | 중립. 정답 아닌 결과끼리 바뀜. 정답 위치 같음. |
| S2 | 평면 | `common-heavy-1` | 중립. 정답 10위에서 9위. |
| S2 | 평면 | `korean-variant-0`, `-2`, `top-k-pressure-2` | 중립(따로 적음). 대조군이 정답도 대조군도 아닌 결과를 밀어냄. |
| S2 | 평면 | `top-k-pressure-1` | 중립. 대조군끼리 바뀜. |
| S2 | 평면 | `common-heavy-0`, `-2`, `common-only-1`, `-2` | 중립. 정답 아닌 결과끼리 바뀜. |
| S3 | rerank | `common-heavy-1` | 개선. BM25 2위 정답이 창 밖에서 10위로 옴. |
| S3 | rerank | `top-k-pressure-0`, `-1`, `-2` | 중립. 대조군끼리 바뀜. |
| S3 | rerank | `common-heavy-0`, `common-only-0`, `-1`, `-2` | 중립. 정답 아닌 결과끼리 바뀜. |
| S3 | 평면 | `common-heavy-1`, `whole-scope-0` | 개선. BM25 2위 정답이 창 밖에서 10위로 옴. |
| S3 | 평면 | `korean-variant-0`, `-1`, `-2`, `top-k-pressure-2` | 중립(따로 적음). 대조군이 정답도 대조군도 아닌 결과를 밀어냄. |
| S3 | 평면 | `top-k-pressure-0` | 중립(따로 적음). 대조군 둘이 들어오고 대조군 하나와 정답도 대조군도 아닌 결과 하나가 나감. |
| S3 | 평면 | `top-k-pressure-1` | 중립. 대조군끼리 바뀜. |
| S3 | 평면 | `common-heavy-0`, `-2` | 중립. 정답 아닌 결과끼리 바뀜. |

- 퇴행으로 판정한 질의는 없다.
