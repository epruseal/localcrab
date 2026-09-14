# 게이트 레시피

이 문서는 이 저장소의 게이트(설치 확인, import 경로 확인, 린트, 테스트,
회귀 대사)를 저장소만 가지고 처음부터 실행하는 원시 명령을 모은다. `make`
타깃은 편의 축약이며 실제 게이트 판정의 정본은 아래 명령이다. 세션 밖
개인 메모리에만 있던 절차를 여기로 옮겨 담아, 저장소만 본 사람이나
에이전트가 명령을 추측하지 않게 한다.

값을 문서에 박지 않는다. 대신 재현 명령·타깃 이름·절 제목을 적는다. 예외는
동작 서술(무엇이 무엇을 어떻게 하는가)과 포트·환경변수 이름·종료 코드처럼
코드가 계약으로 고정한 값뿐이다 — 이 값들은 코드가 바뀌지 않는 한 안 썩는다.

## 목차

1. [설치와 사전 확인](#1-설치와-사전-확인)
2. [import 경로 확인](#2-import-경로-확인)
3. [린트](#3-린트)
4. [전체 스위트 실행과 필수 환경변수](#4-전체-스위트-실행과-필수-환경변수)
5. [상존 실패의 성질과 재현 명령](#5-상존-실패의-성질과-재현-명령)
6. [회귀 대사](#6-회귀-대사)
7. [CI·Makefile 대응표(부록)](#7-cimakefile-대응표부록)

## 1. 설치와 사전 확인

```bash
pip install -e ".[dev,pg]"
python -c "import opencrab, sqlite_vec, chromadb, pytest"
```

`opencrab`은 이 저장소 자신이고, `sqlite_vec`·`chromadb`는 `pyproject.toml`의
기본 `dependencies`다. `pytest`는 `[project.optional-dependencies].dev`
소속이라 `[dev,pg]` 설치 없이는 애초에 없다 — 넷 중 하나라도 import가
실패하면 위 설치 명령이 온전히 끝나지 않은 것이다.

## 2. import 경로 확인

```bash
cd /tmp && python -c "import os, opencrab; print(os.path.realpath(opencrab.__file__))"
```

cwd에 `opencrab/`이 없는 중립 디렉터리(예: `/tmp`)에서 대상 워크트리 venv의
python으로 실행하고, 출력 경로가 그 워크트리를 가리키는지 확인한다. 같은
위치에서 `python -P`(Python 3.11+, safe-path)로도 동일 확인이 가능하다.

**대상 워크트리 자신의 cwd에서 실행하면 안 되는 이유**: `python -c`는
`sys.path[0]`에 cwd를 먼저 넣는다. 그 cwd 자체에 `opencrab/` 소스 디렉터리가
있으면 검사 대상과 검사 기준이 같아져, 무엇을 실행해도(다른 venv, 심지어
opencrab 미설치 시스템 python도) 통과해버리는 자기충족 오류가 된다.

참고: `.github/workflows/agent-plugin.yml`의 `wheel-install` 잡(중립
디렉터리 + 격리 venv로 import를 확인)이 같은 패턴의 기존 사례다.

## 3. 린트

```bash
ruff check .
```

`make lint`(`ruff check opencrab tests`)와 다른 점은 `pyproject.toml`의
`[tool.ruff]`에 사용자 exclude 설정이 없어 `.`가 저장소 전체(ruff 기본
제외·ignore 규칙은 그대로 적용된 상태)를 보는 반면 `make lint`는 두
디렉터리로 한정된다는 것이다. 정확히 어느 트리가 빠지는지는 코드 트리
구조가 바뀌면 달라지므로 목록을 박지 않고, 다음 명령으로 그때그때
확인한다:

```bash
diff <(ruff check --show-files . 2>&1 | sort) \
     <(ruff check --show-files opencrab tests 2>&1 | sort)
```

(위반 건수를 세는 `cut -d: -f1` 방식은 위반이 0건이면 아무 경로도 안
내놓아 성립하지 않는다 — `--show-files`는 위반 유무와 무관하게 검사 대상
파일을 그대로 나열한다.) 게이트는 `ruff check .`를 직접 쓴다.

## 4. 전체 스위트 실행과 필수 환경변수

```bash
pytest tests/ -v
```

- `OPENCRAB_PG_TEST_URL`: PG 파리티 테스트 게이트. DB명이 `_test`로 끝나야
  한다(아니면 세션 전체가 tripwire로 중단된다 — 6번 참고). 값의 예시는
  `Makefile`의 `test-pg` 타깃이 정본이다(이 문서에 리터럴 값을 다시 적지
  않는다). 로컬에 전용 PostgreSQL이 아직 없으면 먼저 준비한다(서비스
  이름·컨테이너 이름·DB 준비 명령은 `docker-compose.yml`과 `Makefile`이
  고정한 계약값이라 안 썩는다):

  ```bash
  docker compose up -d postgres
  docker exec opencrab-postgres createdb -U opencrab opencrab_test  # 최초 1회
  make test-pg  # 위 환경변수를 자동 설정하고 실행(6번의 재현 명령을 직접 써도 된다)
  ```

  CI(`.github/workflows/ci.yml`)도 같은 구성을 서비스 컨테이너로 띄운다(7번
  대응표 참고).
- `OPENCRAB_SMOKE_BIN_DIR`: 에이전트 플러그인 스모크 테스트의 PATH
  오버라이드. 미설정 시 `shutil.which("opencrab")`으로 폴백하며, 폴백도
  실패하면 스킵이 아니라 실패로 처리된다(의도된 설계 — `sys.executable`로
  조용히 대체하지 않는다).
- `--basetemp`: 6번의 회귀 대사 절에서 다룬다(파괴적 동작이라 별도 절보다
  대사 절차 안에서 설명하는 편이 낫다).
- `--cov` 회피: 로컬 전체 스위트 재실행(회귀 대사용)은 `--cov` 없이 돈다.
  이유는 실행 시간뿐이다. 커버리지 게이트(`pyproject.toml`의
  `[tool.coverage.report]` `fail_under`)는 CI 전용이 아니라 `make coverage`
  로 로컬에서도 그대로 발효된다.
- 참고(자동 처리, 직접 지정할 필요 없음): `LOCAL_DATA_DIR`은 테스트가
  무조건 override, `LOCALCRAB_ENV_FILE`은 `setdefault`(직접 지정하면 그
  값이 존중된다).
- `OPENCRAB_SKIP_LIVE_GUARD`: `require_live_data()`는 이 변수를 읽지 않는다
  — 라이브 데이터 가드와 무관하며, 게이트를 도는 사람이 설정하거나 지울
  이유가 없다.

## 5. 상존 실패의 성질과 재현 명령

각 항목은 "무엇이 왜 실패/스킵하는가 + 재현 명령"으로만 적는다. 몇 건인지는
적지 않는다(다음 커밋에 바뀐다).

- **PG 파리티 테스트**: `OPENCRAB_PG_TEST_URL` 미설정 시 자동 skip.
  재현: `pytest tests/ -v -k pg` (env 미설정 상태).
- **에이전트 플러그인 스모크 테스트**: `OPENCRAB_SMOKE_BIN_DIR`도
  `shutil.which("opencrab")`도 못 찾으면 실패(스킵 아님). 재현:
  `pytest tests/test_agent_plugin_smoke.py -v` (PATH에서 `opencrab`
  실행파일을 뺀 상태).
- **통합 테스트(Neo4j·MongoDB·Chroma)**: `OPENCRAB_INTEGRATION=1` 미설정
  시 스킵. 재현: `OPENCRAB_INTEGRATION=1 pytest tests/ -v` (해당 서비스가
  로컬에 없는 상태에서 실행하면 접속 실패로 드러난다).

## 6. 회귀 대사

- **base 규칙**: 별도 base 워크트리에서 동일 env·동일 명령으로 순차 실행
  (동시 실행 금지 — 공유 `opencrab_test` DB 오염). `scripts/select_targets.sh`
  는 이 저장소에 없으므로 전체 스위트 대사로 갈음한다.

  재현 명령은 아래 한 줄이며, base 실행과 작업 실행은 이 줄에서 `<워크트리>`
  자리(cd 대상 경로, 파이썬 인터프리터 경로, 임시 경로·로그 파일명의
  식별자 — 전부 같은 워크트리를 가리키는 동일 값)만 각자의 워크트리
  경로로 바꿔 쓴다. 그 외 자리는 글자 그대로 동일하게 둔다:

  ```bash
  cd <워크트리>
  set -o pipefail
  PYTEST_ADDOPTS= OPENCRAB_PG_TEST_URL=<Makefile test-pg 타깃의 값> \
  OPENCRAB_SMOKE_BIN_DIR=<워크트리>/.venv/bin \
  <워크트리>/.venv/bin/python -m pytest tests/ -v \
    --basetemp=/tmp/<워크트리 식별자>-basetemp \
    2>&1 | tee /tmp/<워크트리 식별자>-run.log
  echo "EXIT:$?" | tee -a /tmp/<워크트리 식별자>-run.log
  ```

  종료 코드를 같은 로그 파일에 `-a`(append)로 이어 적는 이유는 판정
  절차 1번("각 실행의 종료 코드를 기록한다")이 나중에 로그만 보고도
  재확인 가능해야 하기 때문이다 — 터미널에만 찍히고 로그에 안 남으면
  그 순간 지나간 값은 다시 볼 수 없다.

  맨 앞의 `cd <워크트리>`는 생략할 수 없다: `tests/`는 cwd 기준 상대
  경로이고, `python -m pytest`는 cwd를 `sys.path[0]`에 넣으므로 소스
  트리까지 cwd를 따라간다. 2번 절과 반대로 여기서는 cwd가 대상
  워크트리 자신이어야 그 워크트리의 테스트와 소스를 본다. `cd`가
  실패하거나 엉뚱한 디렉터리에 머무르면 `tests/`를 못 찾아 pytest가
  사용법 오류(4)나 미수집(5)으로 끝나며, 이는 아래 판정 절차 2번이 이미
  미완주로 잡는다.

  종료 코드는 `${PIPESTATUS[0]}`(bash 전용 배열, zsh에는 없다 — zsh는
  1-시작 소문자 `$pipestatus`를 쓴다)이 아니라 `set -o pipefail`로 잡는다.
  `pipefail`은 bash와 zsh 양쪽에서 동일한 문법으로 동작해, 실행 셸이
  둘 중 무엇이든 같은 명령이 재현된다(`tee`는 통상 0으로 종료하므로
  `pipefail` 아래에서 `$?`는 파이프 왼쪽인 `pytest`의 종료 코드를 그대로
  낸다).

  `OPENCRAB_SMOKE_BIN_DIR`은 "워크트리 경로만 바꾼다"는 위 규칙의 유일한
  예외다 — 값이 각 워크트리 자신의 `.venv/bin`이어야 하므로 base 실행과
  작업 실행이 서로 다른 값을 갖는다. 다른 워크트리의 `.venv/bin`을 넣으면
  대상 워크트리가 설치한 `opencrab`이 아닌 다른 워크트리의 `opencrab`이
  스모크 테스트에 걸려 대사 자체가 무의미해진다.

  반대로 `OPENCRAB_PG_TEST_URL`은 두 실행에서 반드시 같은 값을 쓴다(정본은
  `Makefile`의 `test-pg` 타깃 — 4번 참고). 한쪽만 이 값을 설정하면 그쪽만
  PG 파리티 테스트를 실행하고 다른 쪽은 전부 skip하므로, 두 집합의 diff가
  환경 차이를 결함으로 오판정한다.

### 예방(고정 호출 형태) — 판정 근거로 쓰지 않는다

두 실행 모두 `PYTEST_ADDOPTS=`를 빈 값으로 명시 설정하고(환경에 숨은
옵션이 몰래 주입되는 경로를 미리 닫는다) `-x`/`--maxfail`/`--collect-only`
를 쓰지 않는다. 이 세 조건은 재현 명령의 고정 형태이지 판정 근거가
아니다. **이것만으로 완주를 보장하지 않는다** — `pyproject.toml`의
`addopts`, `-p` 플러그인, 상위 `conftest.py` 등 같은 일을 하는 경로가 더
있을 수 있고, 전부 나열하는 쪽으로는 닫히지 않는다. 그래서 판정은 아래
처럼 **검출**로 한다: 어느 경로로 조기 종료되든 pytest가 자기 실행
조건을 스스로 보고하는 두 숫자(수집 수·처리 수)가 어긋난다.

### 판정 절차

1. 각 실행의 종료 코드를 기록한다.
2. 종료 코드가 0(전부 통과) 또는 1(실패 있음, 또는 tripwire 중단)이
   아니면 그 자체로 미완주다 — pytest의 종료 코드 계약상 2는 실행
   중단(예: `KeyboardInterrupt`), 3은 내부 오류, 4는 사용법 오류, 5는
   미수집이며, 어느 쪽이든 diff를 내지 않고 원인부터 조사한다.
3. 종료 코드가 1이면 PG tripwire(`tests/conftest.py`)는 DB명이 `_test`로
   안 끝나면 `pytest.exit(...)`로 세션 전체를 즉시 중단하며 이때도 종료
   코드가 1이다. 로그에 고정 마커 `[PG tripwire]`(tripwire가 내는 메시지
   앞부분, 코드가 문자 그대로 보장하는 계약값이라 안 썩는다)가 있는지
   먼저 grep한다(로그 파일명은 위 재현 명령의 `tee` 대상):
   ```bash
   grep -n '\[PG tripwire\]' /tmp/<워크트리 식별자>-run.log
   ```
   있으면 diff를 내지 않고 원인(DB명 오설정)부터 고친다.
4. 마커가 없으면(종료 코드 0이거나, 1이면서 마커 없음) 완주 여부를
   옵션 목록이 아니라 pytest 자신이 보고하는 두 숫자의 대사로 확인한다.
   로그 앞부분의 `collected N item(s)`의 `N`과, 로그의 마지막 줄(정상
   완주 시 pytest가 매번 내는 요약줄, 예: `1 failed, 3 passed in
   0.02s`)에 나오는 카테고리별 수의 합을 비교한다. 합산 대상은
   `_pytest.terminal.KNOWN_TYPES`에서 `warnings`와 `subtests *`를 뺀
   나머지: `failed`, `passed`, `skipped`, `deselected`, `xfailed`,
   `xpassed`, `error`(둘 다 코드가 고정한 목록이라 안 썩는다). `N`과
   합이 다르면(예: `-x`/`--maxfail`/`PYTEST_ADDOPTS` 주입으로 조기
   종료됐지만 겉보기엔 정상 종료된 실행) 완주가 아니므로 diff하지 않고
   원인부터 조사한다. (로그 마지막 줄이 그 요약줄이라는 전제가 깨지는
   경로는 이 저장소에 `pytest.exit` 호출 하나뿐인 PG tripwire뿐이며,
   그 경로는 이미 3번에서 먼저 걸린다 — `git grep -n "pytest.exit"`로
   호출부가 하나인지 그때그때 확인한다.) `N`과 합이 같으면 완주다.

   **알려진 예외(완주인데도 합이 `N`보다 큰 경우)**: teardown 단계에서
   에러가 나면 같은 테스트가 `passed`와 `error` 양쪽에 한 번씩 잡혀
   합이 `N`을 넘을 수 있다(pytest의 report 단위 집계이지 테스트 단위
   집계가 아니라서다 — 실측: 테스트 1개짜리 세션에서 teardown만
   실패시키면 `collected 1 item` 대 `1 passed, 1 error`로 합 2가
   나온다). 이 방향(합 > `N`)이 나오면 조기 종료가 아니라 teardown
   에러부터 의심한다 — 로그의 `ERROR at teardown of` 줄 수만큼 초과분을
   설명할 수 있으면 완주로 판정하고, 설명이 안 되는 초과분이 남으면
   원인부터 조사한다. 반대 방향(합 < `N`)은 이 예외의 대상이 아니며
   그대로 미완주로 본다.
5. 완주가 확인되면 로그의 `FAILED`/`ERROR` 줄 id를 정렬된 집합으로
   (없으면 빈 집합으로) 뽑아 양쪽 다 항상 base와 diff한다(빈 집합끼리도
   diff 대상이다 — "전부 통과"를 diff 생략 사유로 쓰지 않는다). 이때
   요약줄이 `N failed`(N>0)를 보고하는데 로그에 `^FAILED `로 시작하는
   줄이 하나도 없으면 리포트 옵션(`-r` 계열, `--no-summary`)이 깨져
   있다는 뜻이므로 diff를 신뢰하지 말고 옵션부터 고친다 — "리포트
   옵션을 기본값으로 고정했다"는 서술이 아니라 로그에 실제로 `FAILED`
   줄이 나온 것 자체가 그 고정이 걸렸다는 증거다.
6. 수집 단계 에러(`ERROR collecting`)는 0.

`--basetemp`은 pytest가 그 디렉터리를 비우는 파괴적 동작이며, 그 시점은
세션 시작이 아니라 세션 중 `TempPathFactory.getbasetemp()` 최초 호출(첫
임시 경로 요청) 때다. 두 실행(base 워크트리 vs 작업 워크트리)이 같은
경로를 공유하면 나중에 그 시점에 도달한 쪽이 먼저 도달한 쪽의 임시
파일을 지운다 — `/tmp` 하위에 워크트리 식별자를 포함한 고유 이름을 쓰고,
실행 직후(다른 프로세스가 그 경로를 쓰고 있지 않은지 확인한 뒤) 회수한다.

### 역변이

`scripts/qa/mutate_module.py`. 범위는 `--all`(`opencrab/pack/` 전체를
자동 열거하고 등록 누락 시 스스로 실패해 목록을 알려준다 — 정확한 현재
등록/미등록 상태는 이 실행 자체로 확인하며 이 문서에 목록을 박지 않는다)
또는 단일 모듈 모드(임의 모듈 경로 + 대응 테스트를 받는다. 선행 조건:
지정한 테스트가 대상 모듈을 실제로 import해야 하고, 변이 전 baseline
실행이 통과해야 한다):

```bash
python scripts/qa/mutate_module.py <리포루트> --all [결과.json]
python scripts/qa/mutate_module.py <리포루트> <모듈> <테스트>[,<테스트>...] [결과.json]
```

반드시 클론/워크트리 위에서 실행한다(대상 파일을 직접 변형했다가
되돌리는 방식이며, 실행 시작 시 지난 실행이 남긴 `.mutate-backup`을 자동
복구·삭제한다).

**이 도구 자신의 docstring은 실제 출력에 있는 '적용불가' 판정을 판정
목록에서 누락하고, 존재하지 않는 스크립트를 인용하는 부분도 있다 — 이
두 결함은 별도 이슈 #370에 등록됐다.**

`opencrab/pack/` 밖의 코드 변경에 대한 역변이는 도구가 따로 없다. 대상
함수를 직접 편집해 실패(RED)를 재확인한 뒤 편집을 되돌리고,
`__pycache__`를 지운다:

```bash
find . -name '__pycache__' -type d -prune -exec rm -rf {} +
```

변이 전에도 대상 트리의 `__pycache__`를 전부 제거하고, 실행에
`PYTHONDONTWRITEBYTECODE=1`을 둔다.

## 7. CI·Makefile 대응표(부록)

처음 게이트를 도는 사람이 매번 볼 필요는 없지만 드리프트를 놓치면
측정이 조용히 틀리므로 참고 부록으로 유지한다.

| 진입점 | 로컬 | CI(`ci.yml`) | 차이와 게이트 선택 |
|---|---|---|---|
| 린트 | `make lint` 타깃 | `ruff check .` | 3번 참고, 게이트는 `ruff check .` |
| 설치 | `dev-install` 타깃(`[dev]`) | `[dev,pg]` | 게이트는 `[dev,pg]`로 직접 설치. `agent-plugin.yml`은 `[dev]`만 설치하는데 그쪽은 패키징 검증이 목적이라 별개 |
| 테스트 | `test` 타깃 | `--cov=opencrab -o addopts=''` | 4번 참고, 회귀 대사는 `--cov` 없이 |
| 커버리지 | `coverage` 타깃 | 위 `--cov` 포함 실행 | 게이트 값(`fail_under`)은 동일하게 발효, 로컬/CI 전용 구분 아님 |
| 인터프리터 | venv의 `python --version`으로 확인 | `ci.yml`의 `python-version` 설정으로 확인 | 결과 불일치 시 우선 의심 축 |

추가로 CI는 issue80 Neo4j 계열까지 disposable 컨테이너로 띄워 실행하지만
로컬 기본 게이트는 그렇게 하지 않는다(5번과 연결).
