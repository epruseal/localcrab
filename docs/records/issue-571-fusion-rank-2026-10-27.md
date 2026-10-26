# #571 결합 결과의 어휘 레그 상위 2 보장 측정 기록 (2026-10-27)

이 기록은 2026-10-27에 관측한 값이다. 갱신하지 않는다. 설계와 판정 근거는 `docs/issue-571-fusion-rank.md`에 있다.

## 1. 조건

- 코드: 이 브랜치의 분기점(`origin/main`, #556과 #567 포함). probe는 저장소 파일을 바꾸지 않았다.
- 코퍼스: `scripts/search_corpus.py generate --preset <S1|S2|S3> --seed 542`. 질의는 셋 모두 48이다. 적재는 같은 코드의 `scripts/search_baseline.py load`이고 셋 모두 `COMPLETE`였다(노드 20150, 61100, 120000).
- 실행: 생성, 적재, probe를 transient systemd 서비스(`CPUQuota=100%`, `Nice=19`, `IOSchedulingClass=best-effort`, `IOSchedulingPriority=7`, `LimitAS=2G`, `RuntimeMaxSec`, `MALLOC_ARENA_MAX=2`, `PYTHONDONTWRITEBYTECODE=1`, `TMPDIR`은 디스크 경로)로 하나씩 돌렸다. 각 서비스는 python을 실행하기 직전에 같은 PID의 셸에서 주소 공간 상한, cgroup `cpu.max`, nice, ionice, 환경 변수를 파일로 남겼다. 모든 서비스에서 주소 공간 상한 2147483648바이트, `cpu.max` `100000 100000`, nice 19, best-effort 7, `MALLOC_ARENA_MAX=2`였다.
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

평면 병합의 질의별 변경은 구현 뒤 id 대조(이 기록의 뒤 절)에 함께 적는다.
