# #608 REST 프로세스의 접근 로그가 질의 문자열 토큰을 가린다

## 근본원인

#427의 로그 마스킹은 `create_app()`(`opencrab/mcp/http_app.py`)에서만 설치됐다. REST 프로세스(`python -m apps.api.main`)는 `create_app()`을 거치지 않고 `mcp_router()`로 `/mcp` 경로를 마운트하므로 필터가 설치되지 않는다. 이 프로세스는 `allow_query_token=False`라서 `?token=`을 401로 거부하지만, uvicorn은 응답 전에 요청 줄을 기록한다.

재현은 REST 진입점을 격리 HOME과 데이터 디렉터리로 띄우고 가짜 값을 `?token=`으로 보내는 것이다(`tests/test_rest_access_log_redaction.py`의 프로세스 시험). 수정 전에는 두 채널에 값이 남았다.

- `uvicorn.access`: 모든 경로의 요청 줄(`/mcp`, `/api/*`, 없는 경로 포함).
- `uvicorn.error`: WebSocket 핸드셰이크 줄(`"WebSocket /mcp?token=..." 403`). REST 프로세스는 `ws="none"`을 쓰지 않고, uvicorn 기본값이 WebSocket 요청을 받아 핸드셰이크 줄을 기록한다.

## 결정

1. `install_access_log_redaction()`의 호출 지점을 `mcp_router()` 안 한 곳으로 옮긴다. `create_app()`의 호출은 지운다. `create_app()`과 `apps/api/main.py`는 둘 다 `mcp_router()`로 라우트를 만들므로 두 앱이 같은 호출을 공유한다. 이 함수는 멱등이다.
2. `_REDACTED_LOGGERS`에 `uvicorn.error`를 더한다. `server/api.py`로 uvicorn CLI가 띄우는 프로세스는 `ws` 인자를 우리 코드가 정할 수 없으므로 `ws="none"`에 기대지 않고 로거에서 막는다. `opencrab serve`의 `ws="none"`은 그대로 둔다.
3. 필터 본문(`_redact_query`, `_scrub_log_arg`)은 바꾸지 않는다. 마스킹 키는 서버가 읽는 `token` 하나다(`request.query_params.get("token")`이 서버 전체에서 질의 문자열을 읽는 유일한 자리다. 재현: `git grep -n "query_params" -- apps opencrab`).
4. 필터는 로거 이름이 아니라 인자의 모양으로 판정한다. 문자열 인자에 `?`가 있고 그 뒤 질의 쌍의 이름이 `token`이면 값을 가린다. 그래서 `uvicorn.error`의 어떤 레코드든 그런 문자열 인자를 가지면 같이 가려진다(예: `Why?token=x`는 `Why?token=***`). 부작용은 레코드 하나의 인자 값 손실뿐이다. 필터는 레코드를 버리지 않고, 인자 형태가 예상과 다른 레코드는 그대로 통과시킨다. 기존 단위 시험이 이 두 성질을 고정한다.

## 하지 않는 일

- 이미 로그에 남은 값의 정리와 토큰 교체(이슈 범위 밖, 운영 작업).
- 리버스 프록시와 클라이언트 쪽 사본.
- `apps/api/main.py`에서 `uvicorn.run`의 `ws`나 `log_config` 인자 변경.
- 헤더 값의 로그 노출 점검. 기본 INFO 실행에서 앱 코드는 요청 헤더를 로그 인자로 쓰지 않는다(`git grep -n -E "logger\..*(headers|url)" -- apps opencrab/mcp`). DEBUG 이하에서는 `websockets` 라이브러리가 WebSocket 핸드셰이크의 요청 헤더(`Authorization` 포함)를 `uvicorn.error`에 기록한다. 이는 질의 문자열 토큰이 아닌 헤더 토큰의 노출이고 이 변경 이전부터 있던 동작이라 #609로 올렸다.

## 토큰이 남는 다른 경로 점검

| 경로 | 판정 |
|---|---|
| `uvicorn.access` 요청 줄 | 필터가 가림(수정 대상) |
| `uvicorn.asgi` TRACE 스코프 | 필터가 가림(기존). `python -m apps.api.main`은 `uvicorn.run`에 `log_level`을 넘기지 않아 기본 INFO라 이 줄을 쓰지 않는다. uvicorn CLI를 `--log-level trace`로 띄우면 이 줄이 생기고 필터가 가린다 |
| `uvicorn.error` WebSocket 핸드셰이크 줄 | 필터가 가림(수정 대상) |
| 앱 로거(`opencrab.api`, `opencrab.mcp.*`)와 예외 메시지 | 요청 URL, 질의 문자열, 헤더를 로그 인자로 쓰는 호출이 없다. 프로세스 시험이 stdout과 stderr 전체에서 값의 부재를 확인한다 |
| 401 응답 본문과 헤더 | 값을 되돌려주지 않는다(기존 동작) |

## 시험과 역변이 대응표

| # | 시험 | 수정 전 | 검출하는 변이 |
|---|---|---|---|
| 1 | `tests/test_rest_access_log_redaction.py::test_query_token_never_reaches_rest_process_logs`: 실제 REST 진입점에 `?token=<가짜값>`을 `/mcp`, `/mcp/`, `/api/usage`, 없는 경로, WebSocket 요청으로 보낸 뒤 stdout과 stderr에 값이 없고 `token=***`가 있다 | 실패(RED) | `mcp_router`에서 설치 호출 삭제(설치를 `create_app`에만 둠), 대상 로거에서 `uvicorn.access` 삭제, 대상 로거에서 `uvicorn.error` 삭제 |
| 2 | `...::test_mcp_router_installs_the_redactor_on_every_uvicorn_logger`: 필터를 걷어낸 뒤 `mcp_router()`를 두 번 불러 세 로거에 필터가 정확히 하나씩 있다 | 실패(RED) | 설치 호출 삭제, 대상 로거 삭제 |
| 3 | `tests/test_http_access_log_redaction.py::test_install_is_idempotent_and_attaches_both_loggers`(수정): 세 로거 | 실패(RED, `uvicorn.error` 부재) | 대상 로거 삭제 |
| 4 | 기존 `opencrab serve` 프로세스 시험과 필터 단위 시험(변경 없음) | 통과(대조군) | 필터 본문 변이는 이 변경 범위 밖이며 기존 시험이 맡는다 |

변이는 대상 파일을 직접 편집해 시험이 실패함을 확인한 뒤 원본으로 되돌린다(`docs/gate-recipes.md` 역변이 절의 `opencrab/pack/` 밖 절차). 수정 전 관측은 base의 `opencrab/mcp/http_app.py`에 새 시험만 얹어 돌린 결과다.

## 관측 경계 판정

`docs/observability-rules.md` 대조: 새 사용자 작업 경계나 trace를 만들지 않는다. 기록을 더하지 않고 기존 uvicorn 로그의 자격증명 노출을 줄인다. 필터는 레코드를 버리지 않고 예외를 내지 않는다(기존 계약).

## SOLID 판정

- 단일 책임: 마스킹 정의와 설치는 `http_app.py`의 한 함수에 그대로 있다. 호출 지점만 옮긴다.
- 개방 폐쇄: 로거 이름 튜플에 항목 하나를 더한다. 분기를 복사하지 않는다.
- 리스코프, 인터페이스 분리, 의존 역전: 새 타입이나 인자가 없어 해당 없음.

## 가정

- REST 진입점의 마스킹은 로거 필터가 `uvicorn.run()`의 `dictConfig`에 지워지지 않는다는 가정에 기대지 않는다. `python -m apps.api.main`은 `__main__` 실행에서 한 번(로그 설정 전), uvicorn이 `apps.api.main:app`을 다시 import할 때 한 번(로그 설정 후) 설치한다. uvicorn CLI로 띄우는 `server.api:app`은 로그 설정 뒤에 import하며 설치한다. 그래서 프로세스 시험(1번)은 이 가정을 검사하지 못한다. `opencrab serve`는 `create_app()`이 `uvicorn.run()` 앞에서 한 번만 설치하므로 가정에 기댄다. 그 경로는 기존 프로세스 시험이 지킨다.
- uvicorn이 WebSocket 핸드셰이크를 처리해 앱이 거부(403)하면 `uvicorn.error`에 `(클라이언트 주소, 경로+질의 문자열)` 인자의 핸드셰이크 줄이 기록된다. 시험 1번은 업그레이드 요청의 응답 상태 줄이 403이면 양성 대조(`WebSocket /mcp?token=***`)를 단언한다. 401이면(WebSocket 라이브러리가 없어 uvicorn이 업그레이드를 일반 HTTP로 넘긴 경우) 대조를 건너뛰고 값 부재 단언만 건다. 그 밖의 상태 줄과 수신 실패는 시험 실패다.
- 시험 1번의 자식 프로세스는 `.env`와 상속 저장소 설정을 읽지 않는다. 환경은 허용 목록(`PATH`와 임시 `HOME`, `LOCAL_DATA_DIR`, `LOCALCRAB_ENV_FILE=/dev/null`, `PORT`, `PYTHONPATH`)이고, 작업 디렉터리는 `.env`가 없는 임시 디렉터리다(Pydantic Settings의 `env_file`은 `LOCALCRAB_ENV_FILE`과 현재 디렉터리 `.env`뿐이다). `apps/api/main.py`의 `load_dotenv` 두 호출은 시험의 실행기가 무력화한다. `tests/conftest.py`의 격리 값은 공유하지 않는다. `LOCALCRAB_ENV_FILE`은 `setdefault`라 호출자가 실제 파일을 지정할 수 있고 `LOCAL_DATA_DIR`은 세션 공용 디렉터리이기 때문이다.
- 라이브 서비스는 이 변경을 반영하려면 REST 프로세스를 재시작해야 한다. 재시작은 리드가 한다.

## 받아들임 기준

- REST 프로세스에 `?token=<값>`을 보낸 뒤 접근 로그와 stderr에 값이 없다는 시험이 있고, 수정 전에 실패한다.
- `opencrab serve`의 기존 마스킹 시험은 그대로 통과한다.
- 설치 호출은 `mcp_router()` 한 곳이다(`git grep -n "install_access_log_redaction()" -- opencrab apps`).
- 린트가 깨끗하고 전량 회귀 대사에서 base 대비 의도되지 않은 상태 변화가 없다. PR CI가 통과한다.
