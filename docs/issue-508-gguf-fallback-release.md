# #508 로컬 GGUF 폴백의 유휴 해제 설계

## 범위와 판정

원격 임베딩 엔드포인트가 모두 실패하면 `ResilientEmbeddingFunction`이 `LlamaCppEmbeddingFunction`(로컬 GGUF)으로 넘어간다. 폴백은 첫 호출에서 모델을 프로세스 안에 올리고 프로세스가 끝날 때까지 내려놓지 않는다. 원격이 복구돼도 상주분이 남는다. 이 변경은 폴백 EF가 일정 유휴 시간 뒤에 모델을 내려놓고 메모리를 운영체제에 돌려주게 한다. 다시 필요해지면 다음 호출이 모델을 다시 올린다.

이 변경이 하지 않는 일: 인스턴스 간 모델 공유, `n_ctx`, `n_threads`, `n_batch` 조정, 폴백 카운터(`fallback_calls`)를 소비자에 배선하는 일(#89), 환경변수 추가, 다른 EF(원격 EF, Chroma의 로컬 minilm)의 수명 변경.

## 근본원인

`LlamaCppEmbeddingFunction._get_llm()`은 `Llama` 객체를 `self._llm`에 한 번 대입한 뒤 읽기만 한다. 해제하는 경로가 없다. `ResilientEmbeddingFunction`은 primary가 성공하면 폴백을 부르지 않을 뿐이며 폴백 EF에 "쓰이지 않는다"는 신호를 보내지 않는다. 호출이 없는 유휴 구간에는 어느 쪽도 코드가 실행되지 않는다. 호출 경로 안에서 해제를 검사하는 방식은 유휴 구간의 메모리를 돌려받지 못한다. 시간 기반 해제는 호출 밖에서 도는 타이머가 필요하다.

## 측정 A: 상주 비용과 해제 방식

격리 프로세스 하나에서 실제 GGUF로 다음 단계마다 `/proc/self/status`의 `VmRSS`, `RssAnon`, `RssFile`, `VmSwap`, `Threads`를 읽는다.

- S0: `llama_cpp` import 직후.
- S1: `Llama()` 생성 직후. S2: 첫 임베딩 직후. S3: 임베딩 20회 직후.
- S4: `Llama.close()` 직후. S5: 참조 삭제와 `gc.collect()` 직후. S6: `malloc_trim(0)` 직후.
- 적재와 해제를 같은 프로세스에서 3회 반복한다.

상주 비용은 S3에서 S0을 뺀 값이고 익명 메모리와 파일 기반 메모리로 나눠 보고한다. 해제 방식의 판정은 S6에서 S0을 뺀 잔여로 한다. 판정 규칙은 측정 전에 사전 등록한다: (a) 잔여의 총합과 익명 부분이 상주 비용에 비해 작은 값이어야 한다(허용치는 사전 등록 기록에 숫자로 둔다). (b) 반복한 사이클의 잔여가 첫 사이클보다 늘지 않아야 한다. (a)를 못 채우면 프로세스 안 해제는 부족하다고 판정하고 리드에 재정을 요청한다. 수치는 측정 시점과 함께 PR 본문에 둔다.

S4(`close()` 직후)와 S5와 S6의 값을 비교해 해제 순서(`close()` 다음 `malloc_trim(0)`)를 쓸지 정한다. 비교 값은 PR 본문에 둔다.

## 수정

### 상태와 잠금

`LlamaCppEmbeddingFunction`에 다음을 더한다.

- `idle_unload_seconds`(생성자 인자, 기본 120.0). 0 이하이면 해제 경로와 타이머 생성이 모두 꺼진다(종전 동작).
- `clock`(생성자 인자, 기본 `time.monotonic`): 시계. 시험이 대체한다.
- `_state_lock`: 아래 필드를 읽고 쓰는 짧은 임계 구역의 잠금. 이 잠금을 쥔 채 모델 적재, 해제, 임베딩을 하지 않는다.
- `_inflight`(정수): 지금 모델을 쓰는 호출 수.
- `_last_used`(시계 값): 마지막으로 임대를 얻거나 반납한 시각.
- `_timer`(타이머 객체 또는 `None`): 대기 중이거나 실행 중인 해제 타이머 하나. 새 타이머는 `_timer`가 `None`일 때만 만든다. 타이머는 `_timer_factory(delay, fn)`이 만든다. 기본값은 데몬으로 설정한 `threading.Timer`를 돌려주는 함수다. 시험이 이 속성을 대체한다.
- `_timer_gen`(정수): 타이머를 만들 때마다 1 올린다. 타이머 콜백이 자기 세대를 인자로 받는다.
- 기존 `_llm_lock`은 적재와 해제를 직렬화한다. 적재하는 스레드와 해제하는 스레드 모두 이 잠금을 쥔 채 일한다.
- 기존 `_llm_init_owner`는 `_llm_lock`을 쥔 스레드(적재 또는 해제 중)를 가리킨다. 같은 스레드의 재진입 검사에 쓴다.
- 계수기 `load_count`, `release_count`(해제가 예외 없이 끝난 횟수), `release_error_count`(`close()`가 예외를 낸 횟수)와 읽기 전용 속성 `loaded`.

### 임대 얻기

`_acquire(lease)`가 모델을 돌려준다. `lease`가 참이면 모델을 돌려주는 모든 분기가 `_state_lock` 아래에서 `_inflight`를 1 올리고 `_last_used`를 갱신한다.

1. 빠른 경로: `_state_lock` 아래에서 `_llm`이 있으면 위 갱신을 하고 돌려준다. `lease`가 거짓이면 `_state_lock` 없이 `_llm`을 읽어 돌려준다(기존 `_get_llm()` 동작).
2. 재진입 가드: `_llm_init_owner`가 현재 스레드면 `RuntimeError`를 낸다(기존 코드).
3. 적재 경로: `_llm_lock`을 잡고 `_llm_init_owner`를 현재 스레드로 둔다. 잠금 안에서 `_state_lock` 아래 `_llm`을 다시 본다. 있으면 다른 스레드가 먼저 적재한 것이므로 1번과 같은 갱신을 하고 돌려준다. 없으면 `Llama`를 만든다(기존 코드). 만든 뒤 `_state_lock` 아래에서 `_llm`을 대입하고 `lease`면 갱신하고 `load_count`를 올린다. `finally`에서 `_llm_init_owner`를 `None`으로 되돌린다.

`__call__`은 `_acquire(True)`로 임대를 얻고, 텍스트마다 `create_embedding`을 부르고 `l2_normalize`한 뒤(기존 코드), `finally`에서 반납한다. 반납은 `_state_lock` 아래에서 `_inflight`를 1 내리고 `_last_used`를 갱신하며 `idle_unload_seconds > 0`이고 `_timer`가 `None`이면 `idle_unload_seconds` 뒤에 `_on_timer`를 부르는 타이머를 만들어 `_timer`에 둔다. 기존 시험이 쓰는 `_get_llm()`은 `_acquire(False)`다.

### 해제 경로

`_on_timer(gen)`은 `release_if_idle(from_timer=True)`를 부른다. 예외는 로그에 남기고 삼킨다. `finally`에서 `_state_lock` 아래에 `_timer_gen`이 `gen`과 같으면 `_timer`를 `None`으로 되돌린다. 이렇게 하지 않으면 예외가 난 타이머가 `_timer`에 남아 새 타이머를 막는다.

`release_if_idle(from_timer=False)`:

1. `idle_unload_seconds`가 0 이하이면 `False`로 끝낸다.
2. `_llm_init_owner`가 현재 스레드면 `False`로 끝낸다(적재나 해제 중인 스레드가 되돌아온 호출).
3. `_llm_lock`을 잡고 `_llm_init_owner`를 현재 스레드로 둔다. 적재가 끝나기를 기다린다.
4. `_state_lock` 아래에서 판정한다. 먼저 `from_timer`가 참이면 `_timer`를 `None`으로 되돌린다. 이 대입과 아래 판정이 같은 임계 구역에 있으므로, 반납이 만드는 새 타이머와 이 타이머의 재설정이 겹치지 않는다.
   - `_llm`이 없으면 `False`로 끝낸다.
   - `_inflight > 0`이면 `idle_unload_seconds` 뒤로 타이머를 걸고 `False`로 끝낸다. 건다는 말은 `_timer`가 `None`일 때만 만든다는 뜻이다.
   - `idle_unload_seconds - (clock() - _last_used) > 0`이면 그 남은 시간 뒤로 타이머를 걸고 `False`로 끝낸다.
   - 아니면 `_llm`을 지역 변수로 옮기고 `self._llm = None`으로 둔다.
5. `_state_lock`을 푼 뒤 `_llm_lock`을 쥔 채 지역 변수의 `close()`를 부른다(`close` 속성이 없으면 참조만 버린다). 예외가 나면 WARNING으로 남기고 `release_count` 대신 `release_error_count`를 올린다. 지역 참조를 버리고 `malloc_trim(0)`을 시도한다(`libc.malloc_trim`이 없으면 건너뛴다). 예외 없이 끝났으면 `release_count`를 올리고 INFO로 해제를 남긴다.
6. `finally`에서 `_llm_init_owner`를 `None`으로 되돌리고 `_llm_lock`을 푼다. 해제했으면 `True`를 돌려준다.

`close()`가 예외를 내는 경우의 결정: 폴백이 없으면 검색이 멈추므로 가용성을 택한다. 코드는 참조를 버리고 상태를 UNLOADED로 돌리고 WARNING을 남기고 `release_error_count`를 올린다. 다음 호출은 새 모델을 올린다. `Llama.close()`는 `contextlib.ExitStack.close()`이며 ExitStack은 등록된 해제 콜백을 하나가 실패해도 끝까지 호출한 뒤 예외를 올린다. 따라서 예외가 나도 나머지 콜백은 실행된 상태다. 이 코드는 네이티브 자원이 모두 풀렸는지 알 수 없고, 이 경로에서 네이티브 메모리가 일시적으로 겹칠 수 있음을 받아들인다. `release_error_count`가 운영자가 그 경로를 볼 수 있는 신호다. 시험은 가짜 객체로 관측 가능한 범위(참조가 버려지고 상태가 UNLOADED이며 오류 계수가 오르고 다음 호출이 적재한다)만 판정한다.

### 상태 전이

| 상태 | 사건 | 다음 상태 |
|---|---|---|
| UNLOADED (`_llm` 없음) | 호출 | LOADING (`_llm_lock` 점유) |
| LOADING | 적재 성공 | BUSY (호출이 `_inflight`를 올림) |
| LOADING | 적재 실패 | UNLOADED (예외 전파, 잠금 해제) |
| BUSY (`_inflight` > 0) | 호출 시작 | BUSY (`_inflight` 증가) |
| BUSY | 반납으로 `_inflight`가 0 | IDLE |
| IDLE (`_llm` 있음, `_inflight` = 0) | 호출 | BUSY |
| IDLE | 타이머 실행, 유휴 시간 경과 | CLOSING (`_llm_lock` 점유, `_llm` 없음) |
| IDLE | 타이머 실행, 유휴 시간 미경과 | IDLE (남은 시간 뒤 재설정) |
| BUSY | 타이머 실행 | BUSY (`idle_unload_seconds` 뒤 재설정) |
| CLOSING | `close()` 끝(성공 또는 예외) | UNLOADED |
| CLOSING | 호출 도착 | 호출은 `_llm_lock`에서 대기, CLOSING이 끝난 뒤 LOADING |
| LOADING 또는 CLOSING | 같은 스레드의 호출이나 해제 요청 | `RuntimeError` 또는 `False` (상태 변화 없음) |

타이머 필드는 하나다. 타이머 실행이 시작된 뒤 판정 임계 구역까지는 `_timer`가 실행 중인 그 타이머를 가리키므로 반납이 새 타이머를 만들지 않는다. 판정 임계 구역에서 `_timer`가 `None`이 되고 같은 구역 안에서만 재설정이 새 타이머를 만든다.

### 로그

적재 로그는 기존 두 줄을 유지한다. 해제할 때 INFO로 "로컬 GGUF 해제"와 유휴 시간을 남긴다. `close()` 예외는 WARNING이다.

## 호출자와 형제 경로

- `LlamaCppEmbeddingFunction`의 생성 지점은 `opencrab/stores/factory.py`의 `_make_kure_embedding_function` 하나다. 인자를 넘기지 않으므로 기본 `idle_unload_seconds`가 적용된다.
- 호출 지점은 `ResilientEmbeddingFunction._embed_chain`의 `self._fallback(input)` 하나다. 호출 계약(`list[str]` 입력, 벡터 리스트 출력, 예외 전파)은 그대로다.
- 시험이 `_get_llm`, `_llm_lock`, `_llm_init_owner`, `_llm`을 직접 읽는다. 이 이름과 의미를 유지한다.
- 같은 프로세스에 폴백 EF가 여러 개 있을 수 있다(REST 앱과 MCP 컨텍스트가 각자 만든다). 각 EF가 자기 타이머로 자기 모델을 해제한다. EF 사이의 조정은 하지 않는다.
- 형제 경로: 다른 EF(원격 `OpenAIEmbeddingFunction`, Chroma 쪽 로컬 EF)는 이 변경이 건드리지 않는다. 그 EF들의 모델 수명은 이 변경의 범위 밖이다.
- 프로세스가 끝나면 타이머 스레드는 데몬이므로 종료를 막지 않는다. 새 프로세스의 생성자가 `_llm`을 `None`으로 시작한다. 영속 자원은 없다.

## SOLID 판정

단일 책임: 해제 판정과 타이머는 `LlamaCppEmbeddingFunction`이 가진다. 모델 수명이 이 클래스의 책임이고 `ResilientEmbeddingFunction`은 모델 수명을 모른다. 호출자는 하나(`_embed_chain`)이고 쓰는 부분이 같다. 개방 폐쇄와 인터페이스 분리: 해당 없음(분기 복제와 넓은 인자가 없다). 리스코프 치환: 호출자가 기대하는 입력, 반환값, 예외는 그대로다. 해제 뒤 재적재가 같은 벡터를 돌려주는지는 측정 C가 판정한다. 의존 역전: 시계는 생성자 인자 `clock`으로 받고 `Llama`는 기존 시험처럼 `llama_cpp` 모듈 대체로 주입한다.

## 시험 전략

가짜 `Llama`(적재 횟수와 `close` 호출을 세고 일부 호출에서 멈추거나 예외를 낸다)와 가짜 시계를 쓴다. 판정은 사건 순서로 하고 시간 대기는 종료 조건으로만 쓴다. 해제 판정이 실제로 실행됐음을 `release_if_idle`의 반환값이나 인스턴스에 씌운 기록 래퍼로 관측한다. 시간이 지났다는 사실만으로 판정이 돌았다고 보지 않는다.

- 유휴 해제: 마지막 호출 뒤 유휴 시간이 지나기 전의 `release_if_idle()`은 `False`이고 `close`가 불리지 않는다. 지난 뒤는 `True`이고 `close`가 한 번 불린다. `loaded`가 거짓이 된다.
- 재적재: 해제 뒤 호출이 모델을 다시 만든다. 가짜 모델이 입력으로 정해지는 값을 돌려주므로 해제 전후 벡터가 같다.
- 사용 중 해제 금지: 호출이 `create_embedding` 안에서 멈춘 동안 시계를 유휴 시간 너머로 옮기고 `release_if_idle()`을 부르면 `False`이고 `close`가 불리지 않는다. 호출이 끝나면 반납이 `_last_used`를 갱신하므로 시계를 옮기지 않은 `release_if_idle()`은 `False`다. 시계를 다시 유휴 시간만큼 옮기면 `True`다. `_inflight` 검사를 지운 구현은 이 시험에서 `True`를 돌려주어 실패한다.
- 잠금을 기다린 호출의 임대: 두 스레드가 첫 호출을 겹치고, 먼저 적재한 스레드가 반납한 뒤에도 늦게 도착한 스레드가 임베딩 중일 때 `release_if_idle()`이 `False`이고 `_inflight`가 1이다. 두 스레드가 모두 끝난 뒤 `_inflight`가 0이다.
- 해제 중 호출: `close` 안에서 멈춘 해제 동안 도착한 호출이 `close`가 끝나기 전에 생성자를 부르지 않고, 끝난 뒤에 적재한다. 생성자 호출 순서와 `close` 종료 순서를 기록해 비교한다.
- 동시 호출: 여러 스레드가 호출과 해제를 반복하는 동안 가짜 모델마다 "닫힌 뒤에 `create_embedding`이 불린 횟수"와 "동시에 열려 있는 모델 수의 최대"를 기록한다. 두 값이 각각 0과 1이다.
- 타이머 겹침: 시험이 `_timer_factory`를 대체해 만들어진 타이머를 기록하고 시험 스레드가 콜백을 직접 부른다. 반납이 반복돼도 대기 중인(아직 콜백을 부르지 않은) 타이머는 `_timer`가 가리키는 하나다. 타이머 콜백이 판정 임계 구역에 들어가기 직전에 반납이 끼어드는 순서를 이벤트로 강제하고, 콜백이 반환한 뒤 대기 중인 타이머가 하나이며 `_timer`가 그것을 가리키는지 확인한다. 해제된 뒤에는 대기 중인 타이머가 0개이고 `_timer`가 `None`이다.
- 실제 타이머: 기본 팩토리로 짧은 유휴 시간 뒤에 해제하는 경로를 한 번 통과한다(이벤트 대기, 한도 있음).
- 사용 중 타이머 실행: 호출이 멈춘 동안 기록한 타이머의 콜백을 부르면 `release_if_idle`이 불리고(기록 래퍼로 관측) `close`는 불리지 않으며 새 타이머가 하나 대기한다.
- 비활성: `idle_unload_seconds`가 0 이하이면 타이머가 만들어지지 않고 `release_if_idle()`이 `False`이며 모델이 닫히지 않는다.
- 실패한 적재: 상태가 UNLOADED이고 다음 호출이 성공한다(기존 시험).
- 실패한 `close`: `close`가 예외를 내면 `release_if_idle()`이 예외를 전파하지 않고 상태가 UNLOADED이며 `release_count`는 그대로이고 `release_error_count`가 1 오르고 다음 호출이 새 모델을 적재한다.
- 재진입: 적재 중 같은 스레드의 해제 요청은 `False`이고 `close` 안에서 같은 스레드의 호출은 `RuntimeError`다. 둘 다 교착 없이 끝난다(한도 있는 대기).
- 기존 #302 시험이 전량 통과한다.
- 시간축 측정(실제 모델)과 벡터 동일성 측정은 아래 측정 B와 C로 한다.

## 받아들임 기준

- 측정 A: 위 절의 절차와 규칙으로 판정한다.
- 측정 B(시간축): 루프백 가짜 원격 엔드포인트(실패와 복구를 제어)와 실제 GGUF로 "원격 장애, 폴백 적재, 원격 복구, 유휴, 해제, 재장애, 재적재, 원격 복구, 해제"를 한 프로세스에서 재현하고 RSS를 시간축으로 기록한다. 판정 규칙을 측정 전에 사전 등록한다. 폴백이 쓰이는 동안 RSS가 적재 수준이고, 마지막 폴백 호출 뒤 `idle_unload_seconds`와 여유 시간이 지난 모든 표본에서 RSS가 적재 전 수준이며, 해제 시각이 `idle_unload_seconds`보다 이르지 않다.
- 측정 C(벡터 동일성): 변경 전 파일로 만든 벡터와 변경 후 첫 적재, 해제 뒤 재적재의 벡터가 같은 입력에서 요소 단위로 같다. 변경 전 파일 자체의 결정성을 먼저 확인한다.
- 새 시험이 위 시험 전략의 경로마다 있고 구현을 되돌리면 실패한다.
- 기존 시험이 전량 통과하고 린트가 깨끗하다.
- 성질에 대한 판정은 측정과 시험이 한다. 이 문서는 알고리즘과 상태 전이만 서술한다.

## 범위 밖

- 인스턴스 간 모델 공유. 프로세스마다 `Llama`를 따로 올리는 구조를 바꾸면 모델 서버나 공유 메모리가 필요하다. 이 변경은 상주 시간을 줄이는 쪽만 다룬다. 동시에 폴백 중인 인스턴스 수만큼 익명 메모리가 겹치는 점은 그대로다. 후속 후보.
- `n_ctx`, `n_threads`, `n_batch` 조정. 임베딩 결과 동일성 검증 비용이 크다. 후속 후보.
- #89(폴백 카운터를 소비자에 배선). 이 변경은 `loaded`, `load_count`, `release_count`를 폴백 EF에 두지만 소비자를 연결하지 않는다. #89는 열어 둔다.
- 해제 유휴 시간의 환경변수화. 필요해지면 후속으로 `config.py`와 `ENV_CONTRACT`에 함께 등록한다.
- Chroma 쪽 로컬 minilm EF의 모델 수명.
