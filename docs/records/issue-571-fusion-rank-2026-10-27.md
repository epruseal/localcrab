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
- 질의별 대조: 하네스 보고서가 담는 레그와 결합의 분류, 누락, 대조군 수(`decoy_above_gold`), 레그가 놓친 정답의 순위(`gold_rank`), 자동 선택 판정을 비교했다. 바뀐 질의는 S1과 S3의 `common-heavy-1` 결합 하나다(`partial_silent`에서 `pass`). S2는 바뀐 질의가 없다. 보고서에는 결합 결과의 정답 위치가 없으므로 그 변화는 7절의 id 대조에 적었다. 예를 들어 S1 `common-heavy-2`의 정답은 rerank에서 10위에서 9위로 올랐다. 결합 `miss` 하나는 모든 보고서에서 `long-tail-1`이다.
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

## 8. 지연 요구치 관측 (2026-10-27, 코드: #574 이전 `origin/main` 위)

이 절은 15절(설계)의 처분 절차를 정하기 전에 관측한 값이다. 코드는 #574의 trace 계측이 들어오기 전이다. 처분의 판정에는 쓰지 않고 동기로만 둔다. 판정 값은 9절에 둔다.

- 대상 요구치: S3 기본 설정의 `concurrent_auto_p95_ms`(상한 5000ms). 같은 적재에서 `run`을 쌍으로 돌렸다.
- 쌍별 `compare` 결과: 작업자의 첫 쌍은 base 4803.4ms, head 4802.2ms로 둘 다 충족이었다. 실행 채널의 첫 쌍은 head 5015.4ms(base는 작업자 값 재사용)였고, 역순 새 쌍은 head 5030.6ms(미충족), base 4882.8ms(충족)였다. S3 축소 상한에서도 head 5003.0ms가 상한을 넘었다.
- 별도 계측 일곱 회(저장소 밖 래퍼, 제품 변경 없음): base 4917.0, 4994.3, 5061.0ms. head 5011.2, 5071.4, 4962.1ms. 변경의 Stage 7 후보만 깊이 1로 돌린 대조군 4943.4ms. 같은 코드의 base도 상한을 넘은 회차가 있고 두 분포가 겹친다.
- 꼬리 질의: 자동 선택 요청 중 `common-heavy-2`의 선택 구간(`resolve_packs`) p95가 약 3.8초에서 4.3초이고, 본 검색 구간은 약 1.2초였다. 선택 구간은 이 변경이 닿지 않는 Stage 7 앞이다. 이 계측의 대조군은 래퍼 우회가 있어 인과 증거로 쓰지 않았다.
- Stage 7 호출당 비용(고정 입력 재생, 호출당 중앙값): 깊이 1이 약 19.4마이크로초, 깊이 2가 약 19.6마이크로초였다. 최댓값은 각각 약 69마이크로초와 74마이크로초였다.
- 한계: 이 값들은 측정 시각, 배경 부하, 표본 수가 서로 다르다. 한 숫자의 순서만으로 원인을 가리지 못한다.

## 9. 재개 뒤 재측정 (2026-11-03, 재기반 코드)

5절부터 7절은 2026-10-27에 #574와 #579가 들어오기 전 `origin/main`을 base로 쟀다. 이 절은 2026-11-03에 #574와 #579를 포함한 `origin/main`으로 이 브랜치를 옮긴 뒤 다시 잰 값이다. 5절부터 7절을 고치지 않고 이 절이 새 base의 값을 더한다.

### 9.1 조건

- 코드: base는 측정 시점의 `origin/main`의 detached 체크아웃과 자기 venv다. head는 이 브랜치를 그 위로 옮긴 코드다. 각 프로세스가 자기 체크아웃의 `opencrab`을 import했음을 설치 직후 확인했다.
- 입력: 1절의 코퍼스와 적재 한 벌(규모마다 하나)을 새 스크래치 디렉터리로 복사해 썼다. `run`은 적재 사본을 실행 디렉터리로 다시 복사한 뒤 그 사본에서만 측정한다.
- 실행 한도: 모든 `run`, B1 프로세스, 전체 스위트를 transient systemd 서비스나 scope에서 하나씩 돌렸다. CPUQuota 100%, 주소 공간 2GiB, nice 19, ionice best-effort 7, `MALLOC_ARENA_MAX=2`, `RuntimeMaxSec`, `PYTHONDONTWRITEBYTECODE=1`, 디스크 `TMPDIR`이다. `run`과 B1 서비스의 속성은 서비스마다 환경 기록 파일에 남겼다. 장치는 8GB RAM, 4코어 aarch64다.
- `OPENCRAB_PG_TEST_URL`은 두 스위트 실행 모두 두지 않았다(사용자 결정, SQLite 기준).

### 9.2 정적 검사, 표적 시험, 전체 스위트

- `ruff check opencrab tests`는 통과했다.
- 표적 시험: 새 시험 파일, 질의 특성 시험, 동일 본문 접기 시험, 질의 조회 시험, 하네스 시험, 장애 주입 시험, trace 시험을 함께 돌려 전량 통과했다.
- 전체 스위트(base, head 순으로 하나씩, 판정은 JUnit XML 구조): 두 요약 모두 `VERDICT:COMPLETE`다. base는 tests 9135, skipped 508, 실패 6이다. head는 tests 9142, skipped 508, 실패 6이다. 실패 id 집합이 같다(`tests/test_issue80_sql_graph.py`의 PG와 Neo4j 런타임 시험 여섯 개, 서비스 필수 시험의 환경 부재). 상태 대조에서 바뀐 항목은 새 시험 일곱 개 추가(모두 pass)뿐이고 삭제와 기존 시험의 상태 변화는 0이다.

### 9.3 #542 하네스 대조 (S1, S2, S3, 기본과 축소 상한)

- 여섯 쌍(규모 셋, 설정 둘) 모두 base와 head의 보고서가 `COMPLETE`이고 `compare`는 `COMPARE: CLEAN`이다.
- 결합 분류: S1과 S3는 두 설정 모두 base `pass 46, miss 1, partial_silent 1`에서 head `pass 47, miss 1`로 바뀐다. 바뀐 질의는 `common-heavy-1` 하나다. S2는 두 설정 모두 base와 head가 `pass 47, miss 1`로 같다. 레그 분류는 모든 보고서에서 BM25, FTS, vector 모두 pass 48이다. 반복 결함 표지(누출, 중복, 범위 오류, 경고 누락, 조용한 부분 결과, 동시 쓰기 누출과 중복)는 모든 head 보고서에서 0이다. base의 S1과 S3 보고서에서는 조용한 부분 결과 표지가 5다.
- S3 기본 설정은 아래 9.5절의 번갈아 쌍 두 벌(예열 포함 열 쌍)에서도 열 쌍 모두 같은 분류 변화(`partial_silent`에서 `pass`)였다.

### 9.4 id 대조

5절부터 7절과 같은 probe를 재기반 코드의 base와 head에 다시 돌렸다. 규모 셋, 설정 둘의 질의별 diff가 7절의 기록과 모두 같다. 레그 목록 차이는 0이고 `fusion:`이 아닌 경고 차이도 0이다. 판정 표(7절)는 그대로 유효하다.

### 9.5 지연 요구치 처분 (설계 15절)

판정 방식은 측정 전에 설계 15절에서 고정했다(설계 검증 일곱 회차). 번갈아 쌍을 두 벌 돌렸다. 첫 벌은 규칙을 정하기 전에 `concurrent_auto_p95_ms` 한 지표의 판정을 위해 돌렸고, 이 벌의 값은 동기로만 쓴다. 판정은 두 번째 벌(확증 시리즈)로만 했다. 쌍은 다섯이고 첫 쌍은 예열이라 뺐다. 순서는 첫 쌍부터 (head, base), (base, head), (head, base), (base, head), (head, base)다. 유효 쌍 넷의 `compare`는 모두 `COMPARE: CLEAN`이었다.

첫 벌의 `concurrent_auto_p95_ms`(ms, 유효 쌍 넷): base 4798.1, 4886.1, 5176.2, 4875.2. head 4804.8, 4984.1, 5000.0, 4968.0. 마지막 쌍의 `compare`가 `latency_whole_scope_p95_ms`를 새 미충족으로 냈다. 이 사실이 15절을 모든 지연 요구치로 일반화한 동기다.

확증 시리즈 결과(ms, 유효 쌍 넷의 값, 상한은 보고서의 값):

| 지표 | 상한 | base | head | head 초과 쌍 | base 초과 쌍 | 판정 A | 분류 |
|---|---|---|---|---|---|---|---|
| latency_explicit_single_p95_ms | 800 | 526.7, 525.4, 557.8, 525.8 | 524.6, 522.4, 526.4, 524.4 | 0 | 0 | 성립 | 충족 |
| concurrent_explicit_single_p95_ms | 1600 | 1218.0, 1224.8, 1302.6, 1222.6 | 1264.3, 1229.0, 1222.0, 1215.0 | 0 | 0 | 성립 | 충족 |
| latency_explicit_multi_p95_ms | 2000 | 19.0, 16.9, 17.1, 16.8 | 23.6, 25.2, 18.6, 18.0 | 0 | 0 | 불성립 | 충족 |
| concurrent_explicit_multi_p95_ms | 4000 | 69.3, 70.6, 71.1, 69.6 | 69.1, 70.6, 69.9, 69.6 | 0 | 0 | 성립 | 충족 |
| latency_whole_scope_p95_ms | 2000 | 1990.9, 2006.8, 2086.1, 1992.7 | 1984.7, 1987.7, 2007.8, 1996.0 | 1 | 2 | 성립 | 기존 상태 |
| concurrent_whole_scope_p95_ms | 4000 | 4477.2, 4580.8, 4685.2, 4475.8 | 4429.1, 4514.9, 4517.2, 4486.5 | 4 | 4 | 성립 | 기존 상태 |
| latency_auto_p95_ms | 2500 | 2071.8, 2078.7, 2213.4, 2067.4 | 2071.7, 2063.9, 2070.5, 2067.9 | 0 | 0 | 성립 | 충족 |
| concurrent_auto_p95_ms | 5000 | 4885.9, 4904.6, 5102.8, 4867.3 | 4901.8, 4865.2, 4887.2, 4794.2 | 0 | 1 | 성립 | 충족 |
| cold_first_request_s(ms로 환산) | 15000 | 37.6, 28.5, 28.6, 28.7 | 28.9, 38.6, 49.3, 49.0 | 0 | 0 | 불성립 | 충족 |
| write_p95_ms(기록 대상) | 500 | 731.0, 716.9, 831.7, 725.0 | 723.0, 702.5, 708.7, 722.4 | 4 | 4 | 성립 | 기존 상태(기록) |
| lock_wait_p95_ms(기록 대상) | 2000 | 1.2, 1.2, 1.1, 1.3 | 1.1, 1.2, 1.2, 1.2 | 0 | 0 | 성립 | 충족 |

- 회귀 후보로 분류된 지표는 없다. 비수치나 결측 관측값도 없었다. 제외 지표(`leg_*`, `fresh_*`)를 `compare`가 새 미충족으로 낸 쌍도 없었다.
- 판정 A가 불성립으로 나온 두 지표(`latency_explicit_multi_p95_ms`, `cold_first_request_s`)는 상한을 넘은 쌍이 없어 충족으로 분류되며 설계상 처분할 것이 없다. 관측으로 적는다. 두 지표 모두 상한의 2퍼센트 아래이고, 조용한 B1 한 벌에서 `explicit_multi` 질의 여섯 개의 head와 base 호출당 시간 차이는 통계 넷 모두 0.6ms 아래였다.
- 기존 상태 지표 처분 대상은 `latency_whole_scope_p95_ms`와 `concurrent_whole_scope_p95_ms`다. base도 같은 상한을 넘었다. 원인 소재는 전체 범위 질의의 BM25 레그 지연(#569의 극단 질의 형태)이다. 두 지표의 "base 범위 폭"은 95.2ms와 209.4ms이고 B의 기준 길이는 그 가운데 작은 값(95.2ms)의 10분의 1인 9.52ms다.
- 기록 대상 `write_p95_ms`는 쓰기 지연을 다루는 #565의 관측이다. 이 변경이 닿지 않는 쓰기 경로이며 동시 실행에서의 인과는 검증하지 않았다.

**판정 B2(trace 꼬리 요청):** 하네스 `run`을 `--trace-out`과 함께 base와 head로 한 번씩 돌렸고 trace 검증기는 두 trace를 모두 통과시켰다. 기존 상태 요청 지표 둘의 꼬리 집합(요청 지연이 그 집합의 p95 이상인 요청)에서 `reserve`와 `result_convert` span 시간의 합은 head 실행에서 `latency_whole_scope_p95_ms`가 최대 0.14ms(기준 9.52ms 미만), `concurrent_whole_scope_p95_ms`가 최대 0.26ms(기준 20.9ms 미만)였다. B2는 성립한다. 자동 선택 지표(충족이라 B2 대상이 아니다)의 꼬리 요청에서도 head 실행의 같은 합이 최대 0.15ms였고 선택 구간이 요청 지연의 약 76퍼센트를 차지했다.

**판정 B1(호출 경로 비용, 실제 `HybridQuery.query` 전체 호출, 질의 48개 전부, 프로세스 순서 base, head, head, base, 예열 5회, 측정 25회, 통계 넷):**

- 첫 한 벌: 한 head 프로세스가 모든 질의에서 일관되게 느렸다(나머지 세 프로세스 평균 대비 벽시계 중앙값 비율의 중앙값 1.037, 최솟값 1.027). 같은 결과 id를 내고 이 변경과 무관한 질의도 같은 비율로 느려서 프로세스 수준의 교란으로 판정했다. 이 한 벌에서 head 차이가 기준(9.52ms)을 넘은 질의는 다섯이었고(최대 31.2ms) 그 가운데 하나는 불확정 질의이기도 했다. 이 프로세스가 도는 시각에 같은 장치에서 분석 스크립트 일부가 겹쳤을 가능성이 있다. 겹침을 직접 입증하는 표본은 이 벌에 없다. 교란은 검증 설정의 원인이므로 같은 규칙 그대로 한 벌을 더 쟀다(재실행 사유). 첫 벌의 원문도 판정 근거로 남긴다.
- 조용한 한 벌(다른 측정과 분석을 같이 돌리지 않았고 vmstat 10초 간격과 ps 상위 8개 30초 간격 표본을 남겼다. 측정 구간에 고부하 프로세스는 없었다): head 차이는 모든 질의, 통계 넷에서 기준 미만이었다(최대 8.79ms, 최소 -4.29ms). 불확정 질의는 둘이었다(base 두 프로세스의 반복 오차가 기준 이상: `common-only-1` 9.82ms, `common-only-2` 14.79ms).
- 재측정(불확정 질의만 호출 50회, 같은 순서): `common-only-1`은 확정됐다(반복 오차 6.74ms, head 차이 최대 1.44ms). `common-only-2`는 불확정이 남았다. 이 질의는 약 1.9초가 걸리는 전체 범위 질의이고, 재측정에서 벽시계 중앙값은 base 1932.2ms와 1918.7ms, head 1933.6ms와 1923.8ms였다(반복 오차 13.44ms, 약 0.7퍼센트). head와 base의 평균 차이 최대는 이 질의에서 3.43ms였다.
- 설계 15절의 규칙으로 B1은 불성립이다. 불확정은 head와 base의 차이가 아니라 같은 base 코드 두 프로세스의 반복 오차가 기준 길이보다 큰 것이다. 설계가 허용한 재측정은 이미 썼다.

**처분(리드 재정, 사용자 상시 지시 근거):** B1은 질의 1건이 측정 해상도 한계로 불확정이며, 리드 재정으로 처분했다. 재정의 근거는 다음과 같다. (1) 불확정은 head와 base의 차이가 아니라 같은 코드 두 프로세스의 반복 오차(약 0.7퍼센트)가 기준보다 큰 측정 해상도의 한계다. (2) 그 질의의 head와 base 중앙값 차이는 모든 측정에서 반복 오차 안이다. (3) 판정 A(처분 대상 전 지표에서 성립, 회귀 후보 0)와 판정 B2(꼬리 요청 검사 성립)가 독립된 근거다. (4) 재측정을 더 해도 해상도가 나아진다는 보장이 없고 설계가 허용한 재측정은 이미 썼다. 이 재정은 받아들임 기준과 지연 상한을 줄이지 않는다. 이 처분으로 `latency_whole_scope_p95_ms`와 `concurrent_whole_scope_p95_ms`의 상한 초과를 이 변경이 만들지 않은 기존 상태로 분류한다. 측정 원문은 #569와 #565에 기록한다.
