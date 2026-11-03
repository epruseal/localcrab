# 이슈 596: C3 주입 판정이 러너 속도에 따라 흔들리는 문제

설계 정본은 `docs/issue-542-fault-injection.md`다. 이 문서는 C3 시나리오의 주입 확인이 `not injected: ['C3']`로 간헐 실패한 원인과 수정 설계를 적는다.

## 1. 원인

확인한 원인은 `scripts/search_faults.py`의 신호 주입 방식이다. 하네스가 "자식이 아직 도는 중에 신호를 보냈다"와 "시간 예산이 backfill보다 먼저 끝났다"를 벽시계 경쟁에 맡긴 탓에, 러너 속도에 따라 주입 확인(`NotInjectedError`)이 실패한다. 경쟁 창이 둘이다.

1. 신호 단계(`SIGINT` 25%, `SIGTERM` 50%). 하네스는 `ro_status`를 20ms 간격으로 읽어 색인 문서 수가 목표에 이르면 신호를 보냈다. 목표에 이른 뒤 신호를 보내기까지 `ro_status`를 한 번 더 읽는다(`state_at`). 전체 backfill은 이 장치에서 초 단위로 끝나므로 남은 구간이 짧다. 그 사이에 부모가 읽기나 스케줄링으로 지연되면 자식이 `ready`로 끝난 뒤 신호를 보내게 되어 `alive`가 거짓이고 `state_at`이 `ready`가 된다.
2. `--max-seconds 0.2` 단계. 예산이 단계가 끝날 때만 확인되므로 `0.2` 초 안에 남은 문서를 모두 색인하는 빠른 러너에서는 자식이 `ready`로 끝나 "stopped before ready" 확인이 실패한다. 이 장치에서도 예산과 필요 시간의 차이가 작다(3절의 둘째 재현 명령으로 확인한다). CI 러너는 같은 시나리오(T1)를 이 장치의 절반쯤 걸려 끝냈으므로(2026-11-03 관측) 이 여유가 사라진다.

두 창은 제품 결함 없이 같은 보고 문구(`not injected: ['C3']`)를 낸다(3절의 재현 명령). CI 실패 당시의 단계별 결과는 로그에 남지 않았으므로 실제로 어느 창이 열렸는지는 확정하지 못했다. 시험의 실패 메시지는 이제 실패한 확인을 단계별로 보여 주므로, 같은 실패가 다시 나면 열린 창을 로그에서 가린다.

제품의 취소 경로는 이 조사에서 결함을 찾지 못했다. 소스상 `_tx`가 `BaseException`을 받아 롤백하고(`SIGINT`), `SIGTERM`은 프로세스를 끝내 SQLite가 미커밋 트랜잭션을 버리며, `--max-seconds`는 단계 뒤에만 확인하고 종료 코드 0을 낸다.

이슈의 첫 가설(#574 이후 도입)은 틀렸다. 같은 시험이 #574 이전 main CI에서도 같은 사유로 실패했다(2026-10-25 두 회차). 시험과 시나리오는 #555에서 들어왔다.

K1(`SIGKILL` 25/50/75%)도 같은 폴링 후 신호 패턴이다. 신호 직전 추가 읽기가 없어 창이 좁을 뿐 부모가 지연되면 같은 방식으로 깨진다.

## 2. 수정

판정을 시간이 아니라 사건에 건다.

- 자식 `backfill`에 `--hold-at-docs <N>`을 더한다. 자식은 색인이 N개 문서에 이르는 키를 `_node_bm25_sqlite.reindex_key`로 색인한 직후에 단계 트랜잭션이 열린 채 `held` 줄을 내고 신호를 기다린다(K2의 `--pause-at`과 같은 기법). 시작 문서 수는 자식이 시작할 때 읽기 전용으로 읽는다.
- 하네스는 `held` 줄을 기다린 뒤 신호를 보낸다. 자식이 멈춰 있으므로 backfill이 얼마나 빨리 끝나는지와 무관하게 자식은 살아 있고 상태는 `backfill`이다. 자식 자신의 `alarm`(자식 제한 시간)은 그대로 남아 대기를 끝낸다. 자식은 키 하나를 색인한 뒤에 멈추므로 신호는 항상 미커밋 쓰기가 있는 단계 트랜잭션 안에 떨어진다. "부분 단계 없음" 확인이 신호마다 롤백 경로를 실제로 거친다.
- C3 세 번째 단계는 `--max-seconds 0`을 쓴다. 예산 확인이 첫 단계 뒤에 일어나므로 러너가 얼마나 빨라도 자식은 한 묶음만 처리하고 `backfill` 상태로 종료 코드 0을 낸다.
- 공통 부분은 `Scenario.hold_backfill` 한 곳에 둔다. C3와 K1이 이를 쓴다. `Scenario.wait_progress`는 R1(쓰기와 동시 backfill)이 계속 쓴다.

호출자 전수 검사: `wait_progress`의 호출자는 C3, K1, R1 셋이었다. R1은 신호를 보내지 않고 `--step-delay`로 쓰기와 backfill의 겹침을 만들며, 겹침을 주입 조건으로 검사한다. 자식의 단계 대기가 겹침 구간의 하한을 정하므로 CPU 속도에는 기대지 않는다. 부모가 그 구간보다 오래 멈추면 겹침이 비는 잔여 위험은 남으며, R1은 이번 변경으로 달라지지 않는다. `--max-seconds`를 쓰는 호출자는 C3 하나다.

SOLID 판정: 새 단위는 함수 하나(`hold_backfill`)와 actor 옵션 하나다. 호출자 둘이 같은 인자로 쓴다. 분기를 늘리는 데이터화나 추상화는 없다.

## 3. 시험

- `tests/search_faults_timing_child_596.py`는 하네스를 감싼 자식이다. 부모 쪽 `ro_status`가 읽은 뒤 지정한 시간만큼 멈추게 하고(부모 지연), `--max-seconds` 예산에 배수를 곱한다(빠른 러너). 자식 프로세스는 건드리지 않는다.
- `tests/test_search_faults.py::test_signal_injection_does_not_depend_on_runner_speed`는 두 경우로 그 자식을 돌려 `COMPLETE`를 요구한다. `parent_stall`은 C3와 K1을 부모 지연 아래에서, `fast_runner`는 C3를 예산 배수 아래에서 돈다. 수정 전 코드에서는 둘 다 `not injected`로 실패하고 수정 뒤에는 통과한다. 실패 메시지에는 실패한 확인이 단계별로 실린다.
- 역변이: 하네스를 폴링 방식(`wait_progress`)이나 `--max-seconds 0.2`로 되돌리면 위 시험이 실패해야 한다.

재현 명령(기준선 코퍼스는 `docs/gate-recipes.md`의 S0 절차로 만든다):

```bash
python tests/search_faults_timing_child_596.py 0.5 1  run --corpus <코퍼스> --work <적재> --run-root <루트> --out <보고서> --scenarios C3,K1   # 부모 지연
python tests/search_faults_timing_child_596.py 0   2  run --corpus <코퍼스> --work <적재> --run-root <루트> --out <보고서> --scenarios C3      # 예산 배수
```

## 4. 범위 밖

- 제품 코드는 바꾸지 않는다. 취소 경로와 `--max-seconds` 동작은 설계대로다.
- 시험 시간과 다른 시나리오(T 계열, D 계열)의 시간 의존 점검은 이 이슈가 다루지 않는다.
