# 게이트 레시피

이 문서는 이 저장소의 게이트(설치 확인, import 경로 확인, 린트, 테스트,
회귀 대사)를 저장소만 가지고 처음부터 실행하는 원시 명령을 모은다. `make`
타깃은 편의 축약이며 실제 게이트 판정의 정본은 아래 명령이다. 세션 밖
개인 메모리에만 있던 절차를 여기로 옮겨 담아, 저장소만 본 사람이나
에이전트가 명령을 추측하지 않게 한다.

값을 문서에 박지 않는다. 대신 재현 명령, 타깃 이름, 절 제목을 적는다. 예외는
동작 서술(무엇이 무엇을 어떻게 하는가)과 포트, 환경변수 이름, 종료 코드처럼
코드가 계약으로 고정한 값뿐이다. 이 값들은 코드가 바뀌지 않는 한 안 썩는다.

## 목차

1. [설치와 사전 확인](#1-설치와-사전-확인)
2. [import 경로 확인](#2-import-경로-확인)
3. [린트](#3-린트)
4. [전체 스위트 실행과 필수 환경변수](#4-전체-스위트-실행과-필수-환경변수)
5. [상존 실패의 성질과 재현 명령](#5-상존-실패의-성질과-재현-명령)
6. [회귀 대사](#6-회귀-대사)
7. [CI와 Makefile 대응표(부록)](#7-ci와-makefile-대응표부록)

## 1. 설치와 사전 확인

워크트리마다 독립된 가상환경을 먼저 만든다. 6번의 재현 명령이
`<워크트리>/.venv/bin/python`을 직접 지정하므로, 이 자리를 건너뛰면 그
경로가 없거나 현재 활성 인터프리터에 잘못 설치된다:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,pg]"
.venv/bin/python -c "import opencrab, sqlite_vec, chromadb, pytest"
```

`opencrab`은 이 저장소 자신이고, `sqlite_vec`와 `chromadb`는 `pyproject.toml`의
기본 `dependencies`다. `pytest`는 `[project.optional-dependencies].dev`
소속이라 `[dev,pg]` 설치 없이는 애초에 없다. 넷 중 하나라도 import가
실패하면 위 설치 명령이 온전히 끝나지 않은 것이다.

## 2. import 경로 확인

```bash
cd /tmp && <워크트리>/.venv/bin/python -c "import os, opencrab; print(os.path.realpath(opencrab.__file__))"
```

cwd에 `opencrab/`이 없는 중립 디렉터리(예: `/tmp`)에서 대상 워크트리 venv의
python으로 실행하고, 출력 경로가 그 워크트리를 가리키는지 확인한다. 활성화
없이 venv 인터프리터 경로를 직접 지정하는 이유는 활성화에 기대면 다른 셸에서
잘못된 venv가 이미 활성 상태일 때 그 사실이 드러나지 않기 때문이다. 같은
위치에서 `<워크트리>/.venv/bin/python -P`(Python 3.11+, safe-path)로도 동일
확인이 가능하다.

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
제외와 ignore 규칙은 그대로 적용된 상태)를 보는 반면 `make lint`는 두
디렉터리로 한정된다는 것이다. 정확히 어느 트리가 빠지는지는 코드 트리
구조가 바뀌면 달라지므로 목록을 박지 않고, 다음 명령으로 그때그때
확인한다:

```bash
diff <(ruff check --show-files . 2>&1 | sort) \
     <(ruff check --show-files opencrab tests 2>&1 | sort)
```

(위반 건수를 세는 `cut -d: -f1` 방식은 위반이 0건이면 아무 경로도 안
내놓아 성립하지 않는다. `--show-files`는 위반 유무와 무관하게 검사 대상
파일을 그대로 나열한다.) 게이트는 `ruff check .`를 직접 쓴다.

## 4. 전체 스위트 실행과 필수 환경변수

```bash
pytest tests/ -v
```

- `OPENCRAB_PG_TEST_URL`: PG 파리티 테스트 게이트. DB명이 `_test`로 끝나야
  한다(아니면 세션 전체가 tripwire로 중단된다. 6번 참고). 값의 예시는
  `Makefile`의 `test-pg` 타깃이 정본이다(이 문서에 리터럴 값을 다시 적지
  않는다). 로컬에 전용 PostgreSQL이 아직 없으면 먼저 준비한다(서비스
  이름, 컨테이너 이름, DB 준비 명령은 `docker-compose.yml`과 `Makefile`이
  고정한 계약값이라 안 썩는다):

  ```bash
  docker compose up -d --wait postgres  # healthcheck(pg_isready) 통과까지 대기
  docker exec opencrab-postgres createdb -U opencrab opencrab_test  # 최초 1회
  make test-pg  # 위 환경변수를 자동 설정하고 실행(6번의 재현 명령을 직접 써도 된다)
  ```

  `--wait`이 없으면 컨테이너가 뜨자마자 `createdb`가 실행돼 PostgreSQL이
  아직 연결을 받지 않는 시점과 경합할 수 있다. `docker-compose.yml`의
  `postgres` 서비스가 이미 `pg_isready` 기반 `healthcheck`를 선언해
  뒀으므로(`start_period: 15s`), `--wait`으로 그 신호를 그대로 쓴다.

  CI(`.github/workflows/ci.yml`)도 같은 구성을 서비스 컨테이너로 띄운다(7번
  대응표 참고).
- `OPENCRAB_SMOKE_BIN_DIR`: 에이전트 플러그인 스모크 테스트의 PATH
  오버라이드. 미설정 시 `shutil.which("opencrab")`으로 폴백하며, 폴백도
  실패하면 스킵이 아니라 실패로 처리된다(의도된 설계다. `sys.executable`로
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
- `OPENCRAB_SKIP_LIVE_GUARD`: `require_live_data()`는 이 변수를 읽지 않는다.
  라이브 데이터 가드와 무관하며, 게이트를 도는 사람이 설정하거나 지울
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
- **통합 테스트(Neo4j, MongoDB, Chroma)**: `OPENCRAB_INTEGRATION=1` 미설정
  시 스킵. 재현: `OPENCRAB_INTEGRATION=1 pytest tests/ -v` (해당 서비스가
  로컬에 없는 상태에서 실행하면 접속 실패로 드러난다).

## 6. 회귀 대사

- **base 규칙**: 별도 base 워크트리에서 동일 env, 동일 명령으로 순차 실행
  (동시 실행 금지: 공유 `opencrab_test` DB 오염). `scripts/select_targets.sh`
  는 이 저장소에 없으므로 전체 스위트 대사로 갈음한다.

  재현 명령은 아래 한 줄이며, base 실행과 작업 실행은 이 줄에서 `<워크트리>`
  자리(cd 대상 경로, 파이썬 인터프리터 경로, 임시 경로, 로그 파일명의
  식별자: 전부 같은 워크트리를 가리키는 동일 값)만 각자의 워크트리
  경로로 바꿔 쓴다. `<Makefile test-pg 타깃의 값>`도 실행 전에 `Makefile`의
  `test-pg` 타깃에서 실제 URL 값을 확인해 치환한다(두 실행 모두 같은
  값을 쓴다: 아래 참고). 그 외 자리는 글자 그대로 동일하게 둔다:

  ```bash
  (
    rm -f /tmp/<워크트리 식별자>-run.log /tmp/<워크트리 식별자>-run.xml
    cd <워크트리> || exit 17
    set -o pipefail
    PYTEST_ADDOPTS= PY_COLORS=0 OPENCRAB_PG_TEST_URL=<Makefile test-pg 타깃의 값> \
    OPENCRAB_SMOKE_BIN_DIR=<워크트리>/.venv/bin \
    <워크트리>/.venv/bin/python -m pytest tests/ \
      --basetemp=/tmp/<워크트리 식별자>-basetemp \
      --junit-xml=/tmp/<워크트리 식별자>-run.xml \
      2>&1 | tee /tmp/<워크트리 식별자>-run.log
    code=$?
    echo "EXIT:$code" | tee -a /tmp/<워크트리 식별자>-run.log
    exit "$code"
  )
  echo "BLOCK_EXIT:$?"
  ```

  서브셸 마지막 줄을 `exit "$code"`로 맺는 이유는, 그 앞의
  `echo ... | tee -a ...` 파이프라인 자체는 (`echo`와 `tee` 둘 다
  보통 성공하므로) `set -o pipefail` 아래에서도 항상 0으로 끝나기
  때문이다. 그 값을 그대로 서브셸의 종료 코드로 흘려보내면 pytest의
  실제 종료 코드가 사라지고 `BLOCK_EXIT`가 늘 0으로 나온다. 그래서
  파이프 실행 직후 `code=$?`로 pytest의 종료 코드를 먼저 변수에
  붙잡아 두고, 로그에 적은 뒤 그 값으로 명시적으로 `exit`한다.

  종료 코드를 같은 로그 파일에 `-a`(append)로 이어 적는 이유는 판정
  절차 1번("각 실행의 `BLOCK_EXIT` 값을 기록한다")이 나중에 로그만 보고도
  재확인 가능해야 하기 때문이다. 터미널에만 찍히고 로그에 안 남으면
  그 순간 지나간 값은 다시 볼 수 없다(`BLOCK_EXIT` 자체는 로그가 아니라
  터미널에만 남지만, `cd`가 성공한 한 그 값은 로그 마지막 줄의
  `EXIT:$code`와 같으므로 로그만으로도 재확인된다. `cd`가 실패하면
  서브셸 맨 앞의 `rm -f`가 이미 지워 둔 로그 파일 자체가 생기지 않으므로
  비교할 `EXIT:` 줄도 없다. 이 부재 자체가 `BLOCK_EXIT`가 17인 `cd` 실패
  경로라는 신호다. 자세한 근거는 아래 `cd` 실패 처리 문단을 참고한다).

  맨 앞의 `cd <워크트리>`는 생략할 수 없다: `tests/`는 cwd 기준 상대
  경로이고, `python -m pytest`는 cwd를 `sys.path[0]`에 넣으므로 소스
  트리까지 cwd를 따라간다. 2번 절과 반대로 여기서는 cwd가 대상
  워크트리 자신이어야 그 워크트리의 테스트와 소스를 본다. `cd`가
  실패해도 이전 cwd에 우연히 `tests/`가 있으면 pytest는 그 디렉터리를
  대상으로 사용법 오류나 미수집 없이 "정상 종료"해 버릴 수 있다(다른
  워크트리를 잘못 대사하는 것). 그래서 `cd` 자체의 성패를 pytest의
  종료 코드에 얹지 않고 `|| exit 17`로 즉시, pytest의 0~6 계약 값과
  겹치지 않는 값으로 분리해서 낸다. 17은 이 문서가 고른 임의 sentinel이지
  pytest `ExitCode` 계약이 정한 값이 아니다: pytest는 플러그인이
  `pytest.exit(msg, returncode=N)`으로 임의 정수를 낼 수 있게
  열어 둬서(`_pytest/outcomes.py`의 `Exit`), 이론상 어떤 정수 sentinel도
  다른 플러그인의 값과 겹칠 가능성 자체를 원천 차단하지는 못한다. 다만
  이 저장소 자체 코드에는(`.venv`의 pytest 자체 제외) `returncode=17`이나
  `sys.exit(17)` 호출이 전수 grep으로 0건이라 지금은 이 워크트리에서
  충돌이 없다. 다만 이 grep은 이 순간의 관측일 뿐 미래의 모든 충돌
  가능성을 증명하지 않는다.

  `collected N items` 줄의 유무로 `cd` 실패와 pytest 자신의 17을
  가르는 방법은 성립하지 않는다: 어떤 conftest나 플러그인이 수집이
  끝나기 전(`pytest_sessionstart` 등)에 `pytest.exit(msg,
  returncode=17)`을 부르면 pytest는 `collected` 줄을 한 번도 찍지
  않고 종료 코드 17로 끝난다(적대검증에서 이 경로를 실측으로
  재현했다). 그래서 서브셸 맨 앞에 `rm -f /tmp/<워크트리 식별자>-run.log
  /tmp/<워크트리 식별자>-run.xml`을 둔다: 이전 실행의 로그나 xml이 남아
  있으면 `cd`가 실패해 이번 파이프라인이 전혀 안 돌았는데도 지난 실행의
  산출물이 그대로 남아 오판정을 낳기 때문이다(xml이 남으면 아래 판정
  절차 3번의 파일 존재 판정이 이번 실행이 아니라 지난 실행의 결과를
  보고 통과시킨다). 로그를 미리 지워 두면 `cd` 실패 판별 기준은
  **로그 파일 자체의 존재**로 단순해진다: `cd`가 실패하면 `exit 17`이
  그 뒤의 `set -o pipefail`도 `tee`도 전혀 실행하지 않으므로 로그
  파일이 아예 생기지 않는다. 반대로 `cd`가 성공해 파이프라인이 한
  번이라도 시작되면 `tee`가 파일을 열어 만들어 두므로, pytest가 그
  안에서 얼마나 일찍 `pytest.exit`으로 끝나든 로그 파일 자체는
  존재한다(내용이 비어 있거나 짧을 수는 있다). 즉 `BLOCK_EXIT`가
  17이면서 로그 파일이 없으면 `cd` 실패이고, 파일이 있으면(비어
  있어도) pytest 자신이 낸 값이다. 이 전체를 서브셸 `( ... )`로 감싸는
  이유는 그래야 `exit 17`이 그 서브셸만 끝내고 호출한 셸 자체를
  종료시키지 않기 때문이다.

  종료 코드는 `${PIPESTATUS[0]}`(bash 전용 배열, zsh에는 없다. zsh는
  1-시작 소문자 `$pipestatus`를 쓴다)이 아니라 `set -o pipefail`로 잡는다.
  `pipefail`은 bash와 zsh 양쪽에서 동일한 문법으로 동작해, 실행 셸이
  둘 중 무엇이든 같은 명령이 재현된다(`tee`는 통상 0으로 종료하므로
  `pipefail` 아래에서 `$?`는 파이프 왼쪽인 `pytest`의 종료 코드를 그대로
  낸다).

  `OPENCRAB_SMOKE_BIN_DIR`은 "워크트리 경로만 바꾼다"는 위 규칙의 유일한
  예외다: 값이 각 워크트리 자신의 `.venv/bin`이어야 하므로 base 실행과
  작업 실행이 서로 다른 값을 갖는다. 다른 워크트리의 `.venv/bin`을 넣으면
  대상 워크트리가 설치한 `opencrab`이 아닌 다른 워크트리의 `opencrab`이
  스모크 테스트에 걸려 대사 자체가 무의미해진다.

  반대로 `OPENCRAB_PG_TEST_URL`은 두 실행에서 반드시 같은 값을 쓴다(정본은
  `Makefile`의 `test-pg` 타깃이며 4번을 참고한다). 한쪽만 이 값을 설정하면 그쪽만
  PG 파리티 테스트를 실행하고 다른 쪽은 전부 skip하므로, 두 집합의 diff가
  환경 차이를 결함으로 오판정한다.

### 예방(고정 호출 형태): 판정 근거로 쓰지 않는다

두 실행 모두 `PYTEST_ADDOPTS=`를 빈 값으로 명시 설정하고(환경에 숨은
옵션이 몰래 주입되는 경로를 미리 닫는다) `-x`/`--maxfail`/`--collect-only`
를 쓰지 않는다. CLI에 `-v`도 따로 붙이지 않는다: `pyproject.toml`의
`addopts`가 이미 `-v`를 싣고 있어(4번 참고) 중복이다. `PY_COLORS=0`도
명시 설정해 로그를 무색으로 고정한다(사람이 로그를 눈으로 읽을 때
ANSI 코드에 방해받지 않게 하는 목적일 뿐, 아래 판정 절차의 어느
단계도 이 값에 의존하지 않는다).

이 세 조건은 재현 명령의 고정 형태이지 판정 근거가 아니다. 아래 판정
절차는 로그의 진행 줄이나 배너 문자열을 grep하지 않고 `--junit-xml`이
낸 구조화 출력을 읽는다. JUnit XML은 각 `testcase`의 캡처 표준출력을
`system-out` 요소 안에 별도로 담고, 통과/실패 판정은 `failure`/`error`
자식 요소의 유무로, 개수는 `testsuite`의 `tests`/`failures`/`errors`
속성으로 낸다. 실패한 테스트가 캡처 표준출력에 어떤 문자열을 찍어도
(진행 줄 흉내, 배너 흉내, 요약절 헤더 흉내 전부 포함) 그 문자열은
`system-out` 안에만 담기고 개수, `classname`, `name`, `failure`/`error`
속성에는 구조적으로 섞이지 않는다. 이 분리가 캡처 출력 오염을 원천
차단하므로, 텍스트 grep 기반 판정에 필요했던 오염 회피용 플래그
(`--force-short-summary` 등)는 더 필요 없다.

이 문장은 통과/실패 판정과 개수 산출이 캡처 표준출력에 물들지
않는다는 뜻이지, 로그를 전혀 읽지 않는다는 뜻은 아니다. 아래 4번은
개수 자기 정합성을 확인하려고 로그의 `collected N items` 줄 하나를
정규식으로 읽는다. 이 줄은 `system-out`에 담기는 캡처 표준출력이
아니라 pytest 자신이 세션 시작 단계에서 표준출력에 직접 찍는
배너이므로 위 오염 경로(테스트 코드가 캡처 표준출력에 위장 문자열을
찍는 경로)에는 걸리지 않는다. 다만 `pytest_report_header` 훅으로
임의 플러그인이 실제 수집 전에 같은 형태의 문자열을 먼저 찍을 수
있고, 스크립트의 정규식은 첫 매치를 쓴다. 위조된 숫자가 이후 중단
(`pytest.exit` 등)이 남긴 xml `tests` 값과 우연히 맞아떨어지면 4번이
미완주를 완주로 오판정할 수 있다(실측: `pytest_report_header`가
`collected 2 items`를 먼저 찍고 실제 수집은 4건인 상태에서 도중
`pytest.exit(returncode=1)`로 중단하면 `VERDICT:COMPLETE tests=2
skipped=0 failed_or_error=0`이 나온다). 이 저장소의 `conftest.py`들은
`pytest_report_header`를 쓰지 않는다(재현 시점 실측:
`grep -rln 'pytest_report_header' $(find . -name conftest.py)`가 빈
결과). 단일 실행만 보는 4번에는 이 경로가 열려 있지만, 5번의 회귀
diff가 base 대비 `tests=` 감소로 다시 걸러낼 여지가 있다.

### 판정 절차

1. 각 실행의 `BLOCK_EXIT` 값(재현 명령 맨 끝의 `echo "BLOCK_EXIT:$?"`가
   낸 값)을 기록한다.
2. `BLOCK_EXIT`가 17이고 로그 파일(`/tmp/<워크트리 식별자>-run.log`)
   자체가 없으면 `cd`가 실패해 pytest가 아예 실행되지 않은 것이다(서브셸
   맨 앞의 `rm -f`가 매번 로그를 비우므로 이 파일 부재는 이번 실행이
   `tee`까지 한 번도 못 갔다는 뜻이다). 17은 이 문서가 고른 sentinel이지
   pytest `ExitCode` 계약값이 아니다(근거는 위 6절 "17로 즉시" 문단
   참고). 그 자체로 미완주이며 diff를 내지 않고 워크트리 경로부터
   고친다. `BLOCK_EXIT`가 17이 아니거나, 17이면서 로그 파일이 있으면
   (내용이 비어 있어도) 그 값은 pytest 자신의 종료 코드다. 0(전부 통과)
   또는 1(실패 있음, 또는 tripwire 중단)이 아니면 역시 그 자체로
   미완주다. pytest의 종료 코드 계약상 2는 실행 중단(예:
   `KeyboardInterrupt`), 3은 내부 오류, 4는 사용법 오류, 5는 미수집,
   6은 경고 수 초과다(`_pytest/config/__init__.py`의 `ExitCode` 열거값
   전량, 이 워크트리가 설치한 pytest 9.1.1 소스로 확인했다). 어느
   코드든 diff를 내지 않고 원인부터 조사한다.
3. `BLOCK_EXIT`가 17이 아니고 로그 파일도 있는데 xml 파일
   (`/tmp/<워크트리 식별자>-run.xml`)이 없으면, pytest가
   `pytest_sessionfinish` 훅(JUnit XML을 실제로 쓰는 지점)까지 못 가고
   죽은 것이다(세그폴트, OOM-kill, 강제종료 등). 서브셸 맨 앞의
   `rm -f`가 매번 xml도 비우므로 이 부재는 이번 실행이 남긴 것이다.
   그 자체로 미완주이며 diff를 내지 않고 원인부터 조사한다. 참고로
   `pytest.exit(...)`로 세션이 강제 중단되는 경우 가운데 수집이 이미
   끝난 뒤(PG tripwire처럼 세션 스코프 fixture에서 부르는 경우)는 xml
   자체는 만들어진다(`tests="0"`인 빈 testsuite로). 이 경로는 여기서
   걸리지 않고 4번의 개수 대사에서 걸린다. 반대로 수집이 끝나기 전
   (`pytest_sessionstart` 등, 위 6절 참고)에 부르면 `pytest_sessionfinish`
   자체가 안 돌아 xml이 없으므로 이 3번에서 걸린다. 로그에 고정 마커
   `[PG tripwire]`(tripwire가 내는 메시지 앞부분)가 있는지 grep해 두면
   원인 조사가 빠르지만, 이 grep은 캡처 표준출력이 같은 문자열을 찍는
   경우 오탐하므로(테스트 코드가 `print("[PG tripwire] ...")`를
   실행하는 경우) 사람이 원인을 좁히는 참고용일 뿐 판정 근거가 아니다:
   ```bash
   grep -n '\[PG tripwire\]' /tmp/<워크트리 식별자>-run.log
   ```
4. xml 파일이 있으면 아래 스크립트로 완주 여부와 실패/에러 id 집합을
   함께 얻는다. `<워크트리>`는 재현 명령과 같은 워크트리 경로다:
   ```bash
   python3 - /tmp/<워크트리 식별자>-run.log /tmp/<워크트리 식별자>-run.xml <워크트리> <<'PYEOF'
   import re
   import sys
   import xml.etree.ElementTree as ET
   from pathlib import Path

   log_path, xml_path, repo_root = sys.argv[1], sys.argv[2], Path(sys.argv[3])

   log_text = Path(log_path).read_text(errors="replace")
   m = re.search(
       r"collected (\d+) items?(?: / (\d+) deselected)?",
       log_text,
   )
   if not m:
       print("VERDICT:UNTRUSTED reason=no-collected-line")
       sys.exit(1)
   collected, deselected = int(m.group(1)), int(m.group(2) or 0)
   expected = collected - deselected

   suite = ET.parse(xml_path).getroot().find("testsuite")
   tests = int(suite.get("tests"))
   failures = int(suite.get("failures"))
   errors = int(suite.get("errors"))
   skipped = int(suite.get("skipped"))

   # 수집 단계 에러는 classname=""로 들어가고, 개수 우연 일치로
   # 완주를 가장할 수 있으므로(예: 실제 테스트 1개 + 깨진 모듈 1개면
   # collected=1, tests=1로 우연히 같아진다) 개수 비교보다 먼저,
   # 무조건 검사한다.
   collection_errors = [
       tc for tc in suite.findall("testcase")
       if tc.get("classname") == ""
       and (tc.find("failure") is not None or tc.find("error") is not None)
   ]
   if collection_errors:
       print(f"VERDICT:INCOMPLETE reason=collection-error count={len(collection_errors)}")
       sys.exit(1)

   if tests != expected:
       print(f"VERDICT:INCOMPLETE reason=count-mismatch collected={expected} xml_tests={tests}")
       sys.exit(1)

   def reverse_id(classname, name):
       # 수집 단계 에러 전용 형태(classname=="")는 위에서 이미 걸러졌으므로
       # 여기서는 정상 실행 testcase만 온다.
       segs = classname.split(".")
       matches = []
       for k in range(len(segs), 0, -1):
           module = "/".join(segs[:k]) + ".py"
           if (repo_root / module).is_file():
               matches.append((module, segs[k:]))
       if not matches:
           raise ValueError(f"no module path for classname={classname!r} name={name!r}")
       if len(matches) > 1:
           raise ValueError(
               f"ambiguous module path for classname={classname!r}: "
               + ", ".join(m for m, _ in matches)
           )
       module, chain = matches[0]
       return "::".join([module] + chain + [name])

   ids = []
   for tc in suite.findall("testcase"):
       bad = tc.find("failure")
       if bad is None:
           bad = tc.find("error")
       if bad is None:
           continue
       ids.append(reverse_id(tc.get("classname"), tc.get("name")))

   if len(ids) != failures + errors:
       print(
           f"VERDICT:UNTRUSTED reason=id-count-mismatch "
           f"extracted={len(ids)} failures={failures} errors={errors}"
       )
       sys.exit(1)

   print(f"VERDICT:COMPLETE tests={tests} skipped={skipped} failed_or_error={len(ids)}")
   for i in sorted(set(ids)):
       print(f"ID:{i}")
   PYEOF
   ```
   4번의 개수 대사(`collected` 파싱값과 xml `tests` 속성 비교)는 완주한
   실행을 미완주로 오판정할 수 있는 알려진 경우가 둘 있다. 하나는 실행
   자체가 실패했는데 뒤이은 teardown도 에러가 나는 경우로, JUnit XML
   플러그인이 같은 id로 testcase를 하나 더 기록해 xml `tests` 값이
   `collected`보다 커진다(pytest 9.1.1의 이중 계산 보정은 통과+teardown
   에러 조합에만 걸리고 실패+teardown 에러 조합에는 걸리지 않는다).
   다른 하나는 `pytest.skip(allow_module_level=True)`로 모듈 전체를
   수집 단계에서 건너뛰는 경우로, 로그의 `collected N items` 줄은 이
   개수를 세지 않지만 xml에는 그만큼 testcase가 늘어난다. 두 경우
   모두 실제로는 완주한 실행을 `count-mismatch`로 잘못 판정한다.
   위험한 방향(거짓 COMPLETE)이 아니라 안전한 방향(거짓 INCOMPLETE)이라
   판정을 뒤집지는 않지만, 사람이 헛짚어 원인을 찾는 시간을 쓰게 만든다.
   이 저장소의 현재 `tests/`에는 두 패턴 다 실재하지 않는다(이번 실행
   로그가 `collected 6629 items`뿐이고 다른 형태가 없다).

   위 두 경우는 단독으로는 안전한 방향(거짓 INCOMPLETE)이다. 하지만
   세션 도중 `pytest.exit(...)`로 강제 중단되는 경우(위 3번이 참고한
   PG tripwire 경로)와 겹치면 방향이 뒤집힐 수 있다. 중단은 xml
   `tests`를 실제보다 적게 만들고, 위 두 경우는 `collected` 값을
   실제보다 적게 만들거나 xml `tests`를 실제보다 많게 만든다. 두
   어긋남이 우연히 상쇄되면 `tests == expected`가 성립해 미완주
   실행이 `VERDICT:COMPLETE`로 나온다(실측: 모듈 하나를
   `allow_module_level=True`로 건너뛰고 다른 지점에서
   `pytest.exit(returncode=1)`로 중단시키면 계획된 테스트 전부가
   실행되지 않았는데도 `VERDICT:COMPLETE tests=2 skipped=0
   failed_or_error=0`이 나온다). 이 조합은 4번의 개수 대사 하나로는
   못 잡는다. 다만 5번의
   회귀 diff에서 base와 `tests=` 총량을 비교하면 그 감소가 이번
   실행에서만 나고 base에는 없으므로 걸릴 여지가 있다: 단일 실행만
   보는 4번은 못 잡고, base 대비 diff인 5번에서만 걸릴 수 있다는
   뜻이다. 이런 조합은 흔치 않지만 우연한 상쇄가 가능하다는 사실
   자체가 4번 단독의 보장 범위를 좁힌다.

   네 번째로, 테스트 파일이나 디렉터리 이름에 대괄호 `[`가 있으면
   pytest 자신의 `mangle_test_address`가 그 위치에서 먼저 잘라 정상
   실행된 테스트인데도 `classname=""`로 xml에 남는다. 이 형태는 4번의
   수집 에러 검사(`classname==""`이면 수집 에러로 판단)에 걸려, 실제로는
   정상 실행되고 실패한 테스트를 수집 에러로 오분류해
   `VERDICT:INCOMPLETE reason=collection-error`를 낸다(실측: 디렉터리
   이름이 `case[1]`인 경로에 테스트를 두면 재현된다). 이 역시 안전한
   방향(거짓 INCOMPLETE)이며, 사유 문자열이 원인을 오도할 뿐 판정을
   뒤집지는 않는다.

   `reverse_id`는 `classname`(점으로 이어진 모듈/클래스 경로)을 뒤에서부터
   줄여가며 "이 접두어 + `.py`가 실제 파일로 존재하는가"를 검사하는
   후보를 전부 모은다. 후보가 정확히 하나면 그것을 모듈 경로로, 나머지를
   클래스 체인으로 확정한다. 이 저장소의 `tests/` 전량(클래스 기반과
   일반 함수 전부 포함)을 pytest 자신의 `mangle_test_address`
   (`_pytest/junitxml.py`) 순변환과 대사해 왕복 검증했고 불일치
   0건이다(재현 시점의 실제 건수는 `pytest --collect-only -q -o
   addopts=""`로 다시 구한다. 이 숫자는 커밋마다 바뀌므로 이 문서에는
   박지 않는다).

   후보가 하나도 없으면(예: `pytest_internalerror`가 내는
   `classname="pytest", name="internal"`처럼 이 규칙이 예상하지 않은
   형태) `reverse_id`는 `ValueError`를 던진다. 후보가 둘 이상이면(예:
   `tests/foo/bar.py` 옆에 동명 클래스를 딴 `tests/foo/bar/TestBar.py`가
   있어 두 접두어가 모두 실재 파일로 매치하는 경우) 어느 쪽이 맞는지
   판별할 근거가 없으므로 역시 `ValueError`를 던진다. 두 경우 모두
   스크립트가 그 트레이스백과 함께 비정상 종료한다. id를 조용히
   건너뛰지 않는다: 조용히 건너뛰면 실패/에러 id 집합이 부분집합이 되고,
   그 부분집합끼리의 diff가 거짓으로 "같음"을 낼 수 있다. 모호한 매치도
   같은 이유로 조용히 하나를 골라잡지 않고 죽인다.

   `reverse_id`의 후보 생성은 `classname`을 "/"로만 분절해 접두어를
   만든다. 파일명 자체에 마침표가 들어간 모듈(예: `tests/test_a.b.py`)은
   이 분절 방식으로는 후보로 잡히지 않는다. 이 공백은 `reverse_id`까지
   도달하지 못한다: 파이썬 모듈 이름에는 마침표가 들어갈 수 없어
   pytest가 그런 파일을 import 단계에서 수집하지 못하고,
   `classname=""`인 수집 에러 항목으로만 남는다(실측: `test_a.b.py`
   하나만 두고 pytest를 돌리면 `ERROR test_a.b.py` /
   `Interrupted: 1 error during collection`로 죽는다). 이 항목은 4번의
   수집 에러 검사가 먼저 걸러 `VERDICT:INCOMPLETE`를 내므로,
   `reverse_id`가 조용히 틀린 경로를 반환하는 경로는 pytest 자신의
   수집 계약상 도달 불가다.

   pytest의 JUnit XML 작성기(`_pytest/junitxml.py`의 `bin_xml_escape`)는
   `name`/`classname` 속성에 XML로 안전하지 않은 문자(예: 제어문자
   `\x07`)가 있으면 `#x07` 같은 리터럴 텍스트로 바꿔 쓴다. `reverse_id`는
   이렇게 이스케이프된 텍스트에서 원래 raw node id를 되돌리지 못한다.
   이 정보는 xml에 쓰이는 시점에 이미 사라져 `reverse_id` 쪽 코드를
   고쳐도 복구할 수 없고, `--collect-only`로 다시 수집해야만
   되돌아오는데 그러면 이 판정 절차가 xml만으로 완결된다는 전제가
   무너진다. 이 경로는 코드로 닫지 않고 노출로만 남긴다. 테스트나
   parametrize id에 제어 문자가 들어가는 경우는 실무에서 사실상
   나타나지 않는다.

   같은 공백이 디렉터리 이름에 마침표가 있을 때도 있다. 예를 들어
   `tests/v1.2/test_dotdir.py`처럼 디렉터리 이름(`v1.2`) 자체에
   마침표가 있으면, 파이썬 모듈 이름 제약은 파일명에는 걸려도
   `__init__.py` 없는 디렉터리 이름에는 걸리지 않아 이 저장소가 쓰는
   기본 수집 방식(`pyproject.toml`의 `addopts`에 `--import-mode`
   미지정, pytest 기본값 `prepend`)에서 이 파일이 정상 수집되고
   실행된다. 그러면 `reverse_id`가 실제로 불리는데, 후보 생성 루프는
   "/"로만 분절하므로 `tests/v1/2/test_dotdir.py` 같은 존재하지 않는
   조합만 시도해 후보 0건으로 `ValueError`를 던진다(실측:
   `ValueError: no module path for classname='tests.v1.2.test_dotdir'
   name='test_fail'`). 조용한 오매치는 아니지만 판정이 완결되지
   않는다. `tests/a.b/test_real.py`와 그 나이브 분절 경로에 실재하는
   `tests/a/b/test_real.py`가 동시에 있으면 후보가 정확히 하나(틀린
   쪽)로 잡혀 조용히 틀린 id를 반환할 수도 있으나, 이 조합은 pytest가
   두 실경로를 함께 수집해야 성립한다. 이 저장소가 쓰는 기본
   `prepend` 수집 방식은 같은 basename(`test_real`)을 가리키는 두
   실경로가 있으면 그 자체로 수집 오류를 내므로, 조용히 틀린 id를
   반환하는 쪽은 `--import-mode=importlib`를 켜야만 성립하고 이
   저장소의 현재 설정에서는 도달 불가하다.

   pytest는 `record_xml_attribute`나 `record_testsuite_property` 같은
   내장 fixture로 테스트 코드 스스로 xml의 `classname`/`name` 속성을
   덮어쓰게 허용한다. 이 값이 실제 수집 경로와 다르지만 우연히 실재하는
   모듈 경로 하나와 일치하면 `reverse_id`는 후보가 정확히 하나이므로
   의심 없이 그 후보를 반환한다. 반환된 id는 문법적으로 멀쩡하지만
   실제로 실패한 테스트와 다른 테스트를 가리킨다(실측:
   `record_xml_attribute("classname", "tests.test_decoy")`로 덮어쓴
   테스트가 실패하면 `reverse_id`는 `tests/test_decoy.py::test_decoy`를
   돌려주고, 실제로 실패한 파일은 `tests/test_real.py`다). 이 값은
   xml에 쓰이는 시점에 이미 테스트 코드가 바꿔 놓은 뒤라
   `reverse_id` 코드로는 원본과 구분할 방법이 없다. 위 XML 이스케이프
   공백과 같은 이유로 코드로 닫지 않고 노출로만 남긴다. 이 저장소의
   `tests/`는 `record_xml_attribute`도 `record_testsuite_property`도
   쓰지 않는다(재현 시점 실측:
   `grep -rn 'record_xml_attribute\|record_testsuite_property' tests/`가
   빈 결과).

   `VERDICT:UNTRUSTED`는 추출 개수와 `testsuite`의 `failures`+`errors`
   합이 어긋난다는 뜻이다(자기 점검). 이 경우도 diff를 신뢰하지 말고
   원인부터 조사한다.

   이 스크립트는 표준 라이브러리만 쓰고 이 문서 안에서만 쓴다. 반복해서
   쓰는 일이 생기면 `scripts/qa/`로 옮겨 두는 것이 다음 단계다(지금은
   문서 변경만으로 이 저장소의 판정 절차를 완결하기 위해 인라인으로
   둔다. 인라인 상태는 CI가 검사하지 않으므로 저장소 코드가 바뀌면
   내용이 썩을 수 있다는 점에 주의한다).
5. `VERDICT:COMPLETE`가 나오면 그 뒤에 출력된 `ID:` 줄들을 정렬된
   집합으로(없으면 빈 집합으로) 뽑아 양쪽(base와 작업) 다 항상 diff한다
   (빈 집합끼리도 diff 대상이다: "전부 통과"를 diff 생략 사유로 쓰지
   않는다). `VERDICT:INCOMPLETE`나 `VERDICT:UNTRUSTED`가 나오면 diff를
   내지 않고 원인부터 조사한다.

`ID:` 집합 diff만으로는 못 잡는 경우가 있다. 작업 브랜치가 통과하던
테스트를 삭제하거나 조건부로 deselect하면, 그 테스트는 원래도
실패/에러 집합에 없었으므로 양쪽 `ID:` 집합은 그대로 같게 보인다.
이를 잡으려면 `VERDICT:COMPLETE` 줄의 `tests=` 총량도 base와 작업
양쪽에서 항상 비교한다. 작업 쪽 `tests`가 base보다 작으면 diff
결과를 그대로 받아들이지 않고 원인을 조사한다(테스트 파일 삭제, 새
deselect 조건, 리네임에 따른 재수집 실패 등). 그 감소가 PR 본문에
의도로 명시돼 있지 않으면 회귀로 판단한다. `tests`가 base보다 큰
경우(새 테스트 추가)는 정상 증가이므로 조사 대상이 아니다.

`tests=` 비교만으로도 못 잡는 경우가 있다. 통과하던 테스트를
`pytest.mark.skip`이나 `xfail`로 바꾸면 그 테스트는 계속 `testsuite`에
남고 `failure`나 `error` 자식도 없어 `ID:` 집합에 안 잡히며, xml의
`tests` 속성은 통과, skip, 실패, 에러를 모두 더한 값이라 pass가 skip으로
바뀌어도 총량이 그대로다(실측: 테스트 2개짜리 모듈에서 하나를
`@pytest.mark.skip`로 바꾸면 base와 work 둘 다
`tests="2"`이고 `skipped`만 `0`에서 `1`로 바뀐다). 그래서 `VERDICT:COMPLETE`
줄에 `skipped=`도 함께 찍어 base와 작업 양쪽에서 비교한다. 작업 쪽
`skipped`가 base보다 크면 새로 skip 처리된 테스트가 있다는 뜻이니
원인을 조사하고, PR 본문에 의도로 명시돼 있지 않으면 회귀로
판단한다. `skipped`가 base보다 작은 경우(기존 skip이 다시 실행됨)는
정상 감소이므로 조사 대상이 아니다.

`tests=`와 `skipped=` 비교로도 못 잡는 경우가 남는다. 통과하던 테스트
N개를 지우거나 deselect하면서 같은 수의 새 테스트를 추가하면, 새
테스트가 통과하는 한 실패/에러 id 집합도 `tests=`도 `skipped=`도
그대로 같아 diff가 "동일"을 낸다(실측: 통과 테스트 1개를 지우고
이름만 다른 통과 테스트 1개를 더하면 두 실행 다
`VERDICT:COMPLETE tests=2 skipped=0 failed_or_error=0`이고 id 집합도
총량도 같다). 이 잔여 공백을 완전히 닫으려면 실패/에러 id뿐 아니라
xml에 있는 testcase 전량의 id 집합을 base와 작업 양쪽에서 뽑아
diff해야 한다(지금 스크립트는 실패/에러 id만 출력한다). 이 회귀
대사의 선언된 목적은 동작 회귀 검출이고, 통과 테스트의 net-zero
교체는 동작 회귀가 아니라 커버리지 회귀라 이 절차의 목적 밖이다.
전량 id 집합 diff로 커버리지 회귀까지 잡는 확장은 이슈 #383으로
남기고, 이번 절차에는 넣지 않는다. skip 증가는 이번 절차의 `skipped=`
비교가 잡고, 삭제와 추가의 net-zero 교체는 #383이 잡는다. 둘은
커버리지 회귀라는 같은 결함의 두 면이다.

이 5단계는 방어가 서로 겹친다. 예를 들어 3번이 없어 xml 부재 상태로
4번에 넘어가도 xml 파싱 자체가 예외로 죽고, 1번이 종료 코드를 기록하지
않은 상태로 남아도 2번이 그 미기록 상태를 미완주로 막는다. 한 단계만
없앤 반례를 만들어도 다른 단계가 대신 잡아 그 단계 단독의 오판정이
관측되지 않는 경우가 있다. 이것은 결함이 아니라 의도된 중첩이다.

`--basetemp`은 pytest가 그 디렉터리를 비우는 파괴적 동작이며, 그 시점은
세션 시작이 아니라 세션 중 `TempPathFactory.getbasetemp()` 최초 호출(첫
임시 경로 요청) 때다. 두 실행(base 워크트리 vs 작업 워크트리)이 같은
경로를 공유하면 나중에 그 시점에 도달한 쪽이 먼저 도달한 쪽의 임시
파일을 지운다. 이를 피하려면 `/tmp` 하위에 워크트리 식별자를 포함한 고유
이름을 쓰고, 실행 직후(다른 프로세스가 그 경로를 쓰고 있지 않은지 확인한
뒤) 회수한다.

### 역변이

`scripts/qa/mutate_module.py`. 범위는 `--all`(`opencrab/pack/` 전체를
자동 열거하고 등록 누락 시 스스로 실패해 목록을 알려준다. 정확한 현재
등록/미등록 상태는 이 실행 자체로 확인하며 이 문서에 목록을 박지 않는다)
또는 단일 모듈 모드(임의 모듈 경로 + 대응 테스트를 받는다. 선행 조건:
지정한 테스트가 대상 모듈을 실제로 import해야 하고, 변이 전 baseline
실행이 통과해야 한다):

```bash
python scripts/qa/mutate_module.py <리포루트> --all [결과.json]
python scripts/qa/mutate_module.py <리포루트> <모듈> <테스트>[,<테스트>...] [결과.json]
```

반드시 클론/워크트리 위에서 실행한다(대상 파일을 직접 변형했다가
되돌리는 방식이며, 실행 시작 시 지난 실행이 남긴 `.mutate-backup`을
자동으로 복구하고 삭제한다).

**이 도구 자신의 docstring은 실제 출력에 있는 '적용불가' 판정을 판정
목록에서 누락하고, 존재하지 않는 스크립트를 인용하는 부분도 있다. 이
두 결함은 별도 이슈 #370에 등록됐다.**

`opencrab/pack/` 밖의 코드 변경에 대한 역변이는 도구가 따로 없다. 대상
함수를 직접 편집해 실패(RED)를 재확인한 뒤 편집을 되돌리고,
`__pycache__`를 지운다:

```bash
find . -name '__pycache__' -type d -prune -exec rm -rf {} +
```

변이 전에도 대상 트리의 `__pycache__`를 전부 제거하고, 실행에
`PYTHONDONTWRITEBYTECODE=1`을 둔다.

## 7. CI와 Makefile 대응표(부록)

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
