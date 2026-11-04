# #601 REST 인증이 잠긴 토큰 저장소에서 500 대신 503을 낸다

## 근본원인

REST의 `require_auth`(`apps/api/main.py`)는 `verify_token(ctx.sql, presented)`가 던지는 저장소 오류를 처리하지 않는다. 토큰 저장 파일이 잠겨 SQLite busy timeout을 넘기면 `sqlalchemy.exc.OperationalError`가 의존성 밖으로 나가고, 처리기가 없는 예외이므로 요청이 500으로 끝난다. 유효 토큰과 무효 토큰이 같은 결과를 받는다. #580은 MCP 경로(`opencrab/mcp/http_app.py`)에만 같은 상황의 분류와 503 응답을 넣었다.

## 결정

1. 저장소를 일시적으로 쓰지 못해 조회를 끝내지 못한 REST 요청은 503 + `Retry-After` + `Cache-Control: no-store`로 응답한다. `WWW-Authenticate`는 싣지 않는다. 본문은 고정 문구다.
2. 분류 규칙은 `opencrab/auth.py`의 `token_store_unavailable_reason(exc)` 한 곳에 둔다. #580이 `http_app.py`에 둔 `_unavailable_reason`과 `_RETRYABLE_SQLSTATES`를 그대로 옮긴다(본문 변경 없음). `http_app.py`는 이 함수를 가져다 쓴다. REST는 `require_auth` 안에서 호출 시점에 `opencrab.auth`에서 가져온다. 재시도 권고 간격(`TOKEN_STORE_RETRY_AFTER`)도 같은 모듈에 두고 두 경로가 공유한다.
3. 분류하지 않은 오류는 종전처럼 `require_auth` 밖으로 그대로 올라간다(500). MCP 경로는 같은 오류를 `None`, 곧 401로 바꾸지만(#580이 종전 동작으로 둔 것) REST는 종전에 401이 아니라 500이었다. 이 이슈의 받아들임 기준은 "기존 REST 인증 성공과 실패 응답은 바뀌지 않는다"이므로 REST는 종전 동작을 지킨다.
4. `ctx.sql.available`을 보지 않는다. MCP의 `_lookup`은 `available == False` 저장소를 503으로 올리지만, MCP 인증 저장소는 요청마다 연결을 다시 시도하려고 `available == False`인 저장소를 캐시하지 않는다. REST의 `ctx.sql`은 시작 때 한 번 만들고 계속 쓴다. 시작 때 연결 검사나 테이블 생성이 잠금에 걸려 `available == False`가 된 저장소도 엔진은 만들어져 있어서(`SQLStore._connect`는 `create_engine` 뒤에 연결을 검사한다) 오늘 `verify_token`은 그 엔진으로 조회하고 성공한다. 플래그를 조건으로 걸면 이 성공이 503으로 바뀌므로 걸지 않는다. 엔진 자체가 없는 경우(`_engine is None`)는 종전처럼 분류하지 않은 `AttributeError`로 올라간다.
5. 503마다 `opencrab.api` 로거에 WARNING 한 줄을 남긴다. 내용은 고정 사유 코드(`store-busy`, `store-unavailable`)뿐이다. 같은 사유는 10초에 한 번만 남긴다. 단조 시계 읽기, 마지막 기록 시각과의 비교, 갱신, 로그 호출을 같은 잠금 하나 안에서 한다. 시계를 잠금 밖에서 읽으면 잠금을 기다린 스레드가 더 오래된 시각을 저장하고, 로그 호출을 잠금 밖에서 하면 갱신 순서와 기록 순서가 갈린다. 둘 다 같은 사유가 간격 안에 다시 기록되는 경로다(구현 검증 1회차와 설계 검증 3회차 지적). 로그 핸들러는 이 잠금을 쥐지 않으므로 잠금 순환이 없다. 이 잠금은 503 경로에서만 잡힌다. 종전에는 처리되지 않은 예외의 트레이스백이 로그에 남았으므로 로그를 두지 않으면 잠금이 운영자에게 안 보이게 된다.

## 하지 않는 일

- 인증 전용 스레드, 요청별 시한, 대기열 상한. REST의 모든 라우트와 `require_auth`는 동기 함수(`def`)이고 `apps/api/main.py`에 `async def`는 lifespan 하나뿐이다. FastAPI는 동기 의존성을 AnyIO 스레드풀에서 실행하므로 조회가 이벤트 루프를 막지 않는다. SQLite 잠금 대기는 `SQLStore._connect`의 `connect_args["timeout"]`(busy timeout)이 묶는다. PostgreSQL의 연결 풀 대기는 SQLAlchemy의 풀 대기 시한이 묶고 초과하면 분류 대상인 `sqlalchemy.exc.TimeoutError`가 된다. 따라서 #580이 MCP에 둔 전용 워커, 시한, 대기열이 해결한 문제(단일 이벤트 루프 위의 대기 누적)가 REST에는 없다. AnyIO 스레드풀 크기를 넘는 요청이 대기하는 것은 모든 REST 라우트에 공통인 기존 동작이며 이 변경 밖이다.
- `require_auth` 밖의 저장소 접근(라우트 본문의 읽기와 쓰기)이 잠금으로 실패할 때의 응답. 이 이슈의 범위는 인증 조회다.
- 인증 결과 캐시, 회로 차단기, SQLite 저널 모드나 busy timeout 변경.

## 호출자와 형제 경로 전수

재현 명령(저장소 루트):

```bash
git grep -n "verify_token(" -- apps opencrab
git grep -n "Depends(require_auth)" -- apps
```

- `verify_token`을 부르는 곳은 둘이다. MCP의 `_lookup`(#580이 처리)과 REST의 `require_auth`(이 변경). CLI용 `require_local_principal`과 `bootstrap_local_user`는 HTTP 응답 경로가 아니다.
- `require_auth`는 `/healthz`를 뺀 `/api/*` 라우트 전부가 의존한다. 시험이 라우트 목록을 `api.app.routes`에서 열거해 이를 단언하고, `require_auth`가 코루틴 함수가 아님도 단언한다(대응표 2번). REST 앱에 마운트한 MCP 라우트(`mcp_router`)는 `require_auth`를 쓰지 않고 #580의 MCP 인증을 쓴다.

## 응답 계약

| 상황 | 응답 |
|---|---|
| 저장소 정상, 유효 토큰 | 200 (변경 없음) |
| 저장소 정상, 무효 토큰 | 401 + `WWW-Authenticate: Bearer` (변경 없음) |
| 자격 증명 없음, 비 Bearer 헤더, 빈 Bearer 토큰 | 401 (변경 없음). 저장소가 잠겨 있어도 같다. 조회가 필요 없기 때문이다 |
| 분류 대상 저장소 오류(SQLite `SQLITE_BUSY`와 `SQLITE_LOCKED`, 서버형 연결 계열 오류, 연결 풀 대기 초과) | 503 + `Retry-After: 5` + `Cache-Control: no-store`, `WWW-Authenticate` 없음, 고정 본문 |
| 분류 밖 오류(테이블 부재, 파일 열기 실패, 질의 단계의 서버형 오류, 그 밖의 예외) | 500 (변경 없음) |

저장소가 잠긴 동안 무효 토큰의 요청도 503이다. 토큰의 유효성을 판정할 수 없기 때문이다(#580 응답 계약 절과 같다). 분류 조건의 상세는 `docs/issue-580-auth-unavailable.md`의 오류 분류 절이 정한다. 그 절이 가리키는 함수의 위치가 `opencrab/auth.py`로 바뀌었다.

## 관측 경계 판정

`docs/observability-rules.md` 대조: 새 사용자 작업 경계나 trace를 만들지 않는다. 기존 `opencrab.api` 로거에 고정 사유 코드만 기록하고 예외 원문, SQL, 토큰, 토큰 해시는 싣지 않는다. 이 로그는 상시 수집과 집계가 아니다. 한 로그 줄이 한 요청에 대응한다고 가정하지 않는다(같은 사유는 10초에 한 줄).

## SOLID 판정

- 단일 책임: 분류는 `opencrab/auth.py`에서 저장소 오류의 성질만 판정하고, 응답 형식은 두 호출자(MCP, REST)가 각자 정한다. 호출자 둘이 분류만 겹치고 응답 구성과 로그 문구가 갈리므로 이 경계로 나눈다.
- 개방 폐쇄: 값만 바꿔 복사하는 분기가 없다. 해당 없음.
- 리스코프: 함수는 본문 그대로 이동하므로 기존 호출자의 입력과 반환이 같다. `require_auth`의 반환형(`AuthContext`)과 401 예외는 그대로다.
- 인터페이스 분리: 분류 함수는 예외 하나만 받는다. 요청 객체나 저장소를 받지 않는다.
- 의존 역전: 시험이 `opencrab.auth`의 `verify_token`과 분류 함수를 바꿔 끼운다. 구현이 하나뿐이므로 인터페이스 타입을 만들지 않는다.
- 로그 제한 상태(사유별 마지막 시각과 잠금)를 MCP와 공유하지 않는다. MCP의 `_UNAVAILABLE_LOGGED`는 `tests/test_mcp_auth_unavailable.py`가 모듈 속성으로 바꿔 끼우고 로그 문구도 MCP 전용이다. 공유하려면 #580의 시험 fixture를 고쳐야 하므로 이 변경에서는 REST 쪽에 같은 크기의 제한을 따로 둔다. 판정을 보류한 항목이 아니라 범위를 지킨 선택이다.

## 시험과 역변이 대응표

새 파일 `tests/test_rest_auth_unavailable.py`. 변이는 원래 작업 트리가 아니라 폐기할 격리 checkout에서 한다. 수정 전 관측은 base에 이 시험 파일만 얹어 돌린 결과다.

| # | 시험 | 수정 전 | 검출하는 변이 |
|---|---|---|---|
| 1 | 실제 잠금(별도 `sqlite3` 연결의 `BEGIN EXCLUSIVE`): 유효 토큰이 503 + `Retry-After`. 잠금을 풀면 같은 앱이 200. 무효 토큰도 잠금 중에는 503 | 500으로 실패(RED) | `require_auth`의 예외 처리 제거, 분류를 항상 `None`으로 바꿈, `Retry-After`나 `no-store` 제거, `WWW-Authenticate` 추가 |
| 2 | `/api/*` 라우트 전부가 `require_auth`에 의존하고 `require_auth`는 동기 함수다. 열거한 모든 (메서드, 경로)가 busy 오류 주입 때 같은 503 | 라우트 열거 단언은 통과한다(base도 전부 의존). 503 단언은 500으로 실패 | 일부 라우트의 의존성 교체, 한 라우트의 `Depends(require_auth)` 교체, `require_auth`를 `async def`로 바꿈 |
| 3 | 분류: 잠금 계열과 서버형 연결 계열과 풀 대기 초과는 503. 분류 밖 오류(테이블 부재, 파일 열기 실패, `28P01`, 일반 예외)는 500 그대로 | 503 기대 행이 실패한다. 500 행은 base도 통과한다(대조군) | 분류 밖 오류를 503으로 넓힘, 분류 밖 오류를 401로 바꿈, 분류 함수가 일반 예외나 SQLite 비잠금 오류를 포함하도록 넓힘 |
| 4 | 공유: `http_app`이 가리키는 함수 객체가 `opencrab.auth`의 것과 같다. `opencrab.auth`의 분류 함수를 바꾸면 REST 응답이 따라 바뀐다 | 함수 부재로 실패 | REST가 `opencrab.auth`의 분류 함수를 import 시점에 묶음(사본 사용의 대리 변이), MCP가 다른 함수 객체를 가리킴 |
| 5 | 정상 응답 불변: 유효 토큰 200, 무효 토큰 401 + `WWW-Authenticate` + 고정 본문, 자격 증명 없음 401 | base에서도 통과(대조군) | 401 경로를 503이나 500으로 바꿈 |
| 6 | 조회가 필요 없는 요청(자격 증명 없음, 비 Bearer, 빈 Bearer)은 저장소가 잠겨도 401 | base에서도 통과(대조군) | 해당 변이 없음: `verify_token`이 빈 토큰을 저장소 접근 없이 `None`으로 돌려주므로 같은 결과를 내는 변이만 가능하다. 회귀 방지용 대조군이다 |
| 7 | `available == False` 플래그가 조회를 막지 않는다 | base에서도 통과(대조군) | `available` 플래그를 503 조건으로 추가 |
| 8 | 503이 로그 한 줄(고정 사유 코드)을 남기고 반복 요청은 같은 사유를 10초에 한 줄만 남긴다. 줄에 토큰, SQL이 없다. 사유마다 따로 제한하고 간격이 지나면 다시 남긴다 | 시험마다 다르다. 반복 요청 시험은 500 응답이라 첫 503 단언에서 실패한다. 사유별 제한 시험은 base 모듈에 `time`이 없어 시계 대역을 설치하는 줄에서 `AttributeError`로 실패한다. 두 시험 모두 로그 단언에는 도달하지 못하므로 로그 내용의 검출력은 오른쪽 변이로 확인한다 | 로그 제한 삭제, 로그에 예외 원문 기록, 제한을 사유와 무관한 전역으로 바꿈, 제한이 만료되지 않게 바꿈 |
| 9 | 로그 제한이 시계 읽기와 로그 기록을 잠금 안에서 한다(시계 대역과 로그 핸들러가 호출 시점에 잠금이 잡혀 있음을 기록) | base 모듈에 `time`이 없어 시계 대역을 설치하는 줄에서 `AttributeError`로 실패한다. 단언의 검출력은 오른쪽 변이로 확인한다 | 시계 읽기를 잠금 밖으로 옮김, 로그 호출을 잠금 밖으로 옮김 |

회귀: `tests/` 전량을 base와 작업에서 같은 조건으로 돌려 대사한다. 기존 시험의 상태가 같고, 추가는 위 새 파일뿐이어야 한다. #580의 `tests/test_mcp_auth_unavailable.py`는 코드 이동 뒤에도 그대로 통과해야 한다.

## 받아들임 기준

- 토큰 저장 파일이 잠긴 동안 REST 요청이 503 + `Retry-After`를 받는다는 시험이 있고, 수정 전에 실패한다.
- 무효 토큰의 401, 자격 증명 없음, 정상 200 응답은 바뀌지 않는다.
- 분류 함수는 `opencrab/auth.py` 한 곳에 있다.
- 린트가 깨끗하고 전량 회귀 대사에서 base 대비 의도되지 않은 상태 변화가 없다. PR CI가 통과한다.

## 가정

- FastAPI가 동기 의존성을 스레드풀에서 실행한다. 이 가정이 틀리면(의존성이 루프에서 돈다면) busy timeout 동안 이벤트 루프가 막힌다. 그 경우는 #580 같은 워커가 필요하다. 이 변경 전에도 같은 조건이므로 이 변경이 새로 만든 위험은 아니다.
- 운영 `opencrab.db`는 롤백 저널 모드다(#580 가정과 같다). WAL이면 잠금 창이 좁아질 뿐 분류의 정확성은 같다.
- 접속 단계의 PostgreSQL 오류는 `pgcode`를 싣지 않는다(#580 가정과 같다). 틀리면 접속 단계 오류가 분류 밖이 되어 500이 남는다.
