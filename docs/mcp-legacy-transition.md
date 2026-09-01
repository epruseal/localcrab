# MCP legacy 경로 이행 계획 (#136 D절, 추적 이슈 #251)

`docs/mcp-protocol-compat.md` 가 **지금 무엇을 서빙하는가**를 기록한다면, 이 문서는 **legacy 세대를 언제 어떻게 끝낼 수 있는가**를 기록한다. #136(PR #243)이 dual-era 서버를 만들면서 D절(구규격 동작의 제거)을 의도적으로 범위 밖으로 남겼고, 이행 레버로 `MCP_PROTOCOL_VERSIONS` 만 마련했다. 여기서 그 잔여를 추적한다.

**현재 상태: legacy 세대는 활성이며 제거 일정이 없다.** 아래 조건표의 미지수가 채워지기 전에는 제거를 결정하지 않는다.

## 1. legacy 표면 (제거 대상의 전량)

| 표면 | 내용 |
|---|---|
| `initialize` / `notifications/initialized` | handshake 로 세션 의미를 협상. 지원 legacy 버전은 echo, 부재는 `2024-11-05` fallback, 미지·modern 전용 버전은 최신 legacy 제시 |
| `ping` | modern 에서 제거된 메서드 |
| `_meta` 없는 `tools/list` / `tools/call` | era 판정이 요청 바디의 `_meta` 부재로 legacy 로 떨어지는 모든 호출 |
| JSON-RPC 배치 배열 | legacy 전용 LocalCrab 확장 (2026-07-28 은 POST 당 요청 1건) |

## 2. 소비자 compatibility matrix

각 행의 "근거"는 이 저장소에서 확인할 수 있는 것만 적는다. 저장소 밖 소비자는 코드 조사로 이행 가능성을 판정할 수 없으므로 **미관측**으로 표기하고, 추측으로 채우지 않는다.

| 소비자 | 쓰는 legacy 표면 | 근거 | modern 이행 가능성 | 이행 주체 |
|---|---|---|---|---|
| claude.ai 커넥터 | `initialize` handshake + 쿼리 토큰 | **저장소 밖 · 미관측** (`docs/mcp-protocol-compat.md` 의 매트릭스에 기록된 관측 사실이며 소스는 이 저장소에 없다) | 불명 — 커넥터 구현이 2026-07-28 을 말하게 되어야 한다 | 소비자 측 |
| Claude Code / Claude Desktop / Cursor | `initialize` handshake, 일부 legacy `MCP-Protocol-Version` 헤더 동반 | **저장소 밖 · 미관측** | 불명 — 각 호스트의 MCP 클라이언트 구현에 달렸다 | 소비자 측 |
| OpenClaw 호스트 (agent-plugin 번들) | `initialize` echo | `docs/agent-plugin-compatibility.md` 의 호스트별 표 (**그 표가 정본이며 여기서 복제하지 않는다**) | 호스트가 2026-07-28 협상으로 전환해야 한다 | 소비자 측 |
| 대화 reingest hook (`lc_call`) | `_meta`·MCP 헤더 없는 legacy `tools/call` 은 확실. **`initialize` handshake 동반 여부는 미확정** | **저장소 밖 · 미관측이며 저장소의 두 기존 기록이 상충한다**: `docs/mcp-protocol-compat.md` 의 매트릭스는 "initialize 없이" 라 적고, `docs/ingestion-via-mcp-plan.md` 는 같은 `lc_call` 이 `initialize`→`tools/call` handshake 를 구현한다고 적는다. hook 소스가 이 저장소에 없어 어느 쪽이 맞는지 판정할 수 없다 | 가능하지만 **작업량이 미확정** — `_meta` 와 표준 헤더 추가로 끝나는지, handshake 제거까지 필요한지가 위 상충에 달렸다 | 운영자 |
| 로더 계획의 JSON-RPC 배치 | 배치 배열 | `docs/ingestion-via-mcp-plan.md` (계획 단계, 구현 코드 없음) | 설계 시점에 정하면 되므로 제약이 아니다 | 개발 |
| **저장소 소유 CI 참조 클라이언트** | `initialize` → `tools/list` → `tools/call`, `_meta` 없음 | `packaging/agent-plugin/tools/refclient.py` — 이 저장소에서 **legacy 형상 요청을 실제로 조립해 보내는 유일한 코드**. 런타임 wheel 에 포함되지 않는 저작·검증 도구다 | **가능 — 저장소가 스스로 고칠 수 있다** | 개발 (C1) |
| 계약 테스트 | 전 표면 | `tests/` — legacy 계약을 의도적으로 고정하는 파일과, 다른 관심사를 검사하며 legacy 형상을 편의로 재사용하는 파일이 섞여 있다 | 제거 시 함께 정리한다 | 개발 |

위 상충은 **C2 가 사용자에게 물어야 할 항목**이다(§4). 어느 기록이 맞는지는 hook 소스를 가진 쪽만 답할 수 있고, 근거 없이 한쪽을 지우면 정보가 준다 — 그래서 기존 두 문서를 고치지 않고 상충 사실만 여기에 기록한다.

이 표에 없는 내부 호출자는 없다. `apps/api` 는 같은 `mcp_router()` 를 마운트하는 서버 측이고, `scripts/` 와 `apps/web` 에는 MCP JSON-RPC 호출자가 없다.

### 재현 (표를 다시 확인하는 방법)

```bash
# 저장소 안에서 JSON-RPC 요청을 조립해 보내는 코드 (서버 측 응답 조립과 구분된다)
grep -rn '"jsonrpc".*"method"' --include='*.py' opencrab apps scripts packaging
# initialize / ping 표면을 쓰는 테스트 파일
grep -rln '"method": "initialize"\|"method": "ping"' tests/
```

## 3. 남은 era 간 divergence (제거 전까지 유지되는 것)

형상 검증은 #251 에서 modern 공유 검증기(`opencrab/mcp/protocol.py` 의 `validate_tools_call_params`)와 정합시켰다. legacy `tools/call` 도 비문자열 `name`, truthy 비객체 `arguments`, 비객체 `params` 를 dispatch 이전에 -32602 로 거부한다. **정합에서 의도적으로 제외한 것**은 다음 둘이며, D절 제거 시점에 함께 정리한다.

| divergence | 왜 남겼는가 |
|---|---|
| `tools/call` 의 present + **falsy 비객체** `arguments`(`null`/`[]`/`0`/`0.0`/`false`/`""`)를 `{}` 로 정규화해 도구를 실행 | **오늘 도구가 실제로 실행되는 입력**이다. 선택적 필드를 JSON null 로 직렬화하는 클라이언트가 낼 수 있는 형상이고 현재 성공하므로, 거부하면 동작하던 호출이 깨진다. 정합의 안전 규칙("도구를 실행시키던 입력은 하나도 바꾸지 않는다")이 여기서 멈추게 한다 |
| `tools/list` 의 `cursor` 무시 (modern 은 -32602) | #251 이 지목한 name/arguments 축이 아니다 |

응답 봉투 자체(modern 필드 없음, `isError` 없음)는 #136 이 바이트 호환으로 고정했고 그대로다.

### 재현

```bash
.venv/bin/pytest tests/test_mcp_legacy_call_shape.py tests/test_mcp_protocol_2026.py -q
```

## 4. 제거를 승인할 수 있는 조건

기간이 아니라 **조건**으로 정의한다. 기간을 숫자로 만들려면 이 저장소가 갖고 있지 않은 입력이 필요하다(§5).

| # | 조건 | 판정 주체 | 저장소에서 검증 가능? |
|---|---|---|---|
| C1 | 저장소 소유 CI 참조 클라이언트가 modern 형상으로 왕복한다 | 개발 | **예** — modern-only 구성에서 `server/discover` 와 `_meta` 기반 호출을 왕복시키는 스모크로 고정 가능 |
| C2 | 매트릭스의 저장소 밖 소비자 각각에 "이행 완료" 또는 "포기(깨져도 됨)" 결정이 기록돼 있다 | 사용자 | 아니오 |
| C3 | (관측을 채택하면) 연속 가동 구간에서 legacy 계수가 0 이고 그 구간이 관측창 W 이상이다 | 사용자 | 아니오 |
| C4 | modern-only 카나리를 기간 C 동안 운영해 정의된 성공 신호를 만족했다 | 사용자 | 아니오 — **오늘은 판정 불가**(§6) |

**적용되는 조건 집합은 채택한 경로가 정한다**(§5): 안 A 는 C1~C4 전부, 안 B 는 C1·C2·C4(C3 생략, C4 는 §6 (iii) 의 약한 조건으로 낮아진다).

## 5. 오늘 관측 수단이 없다

era 는 요청마다 판정되지만 **어디에도 기록되지 않는다** — 요청별 era 로그도, 카운터도, 메트릭도 없다. 저장소에는 프록시에서 legacy 거부를 분리할 수 있는 신호의 설정·조회 근거가 없다(프록시 설정 자체가 저장소에 없다). 따라서 "아무도 legacy 를 안 쓴다"를 오늘 데이터로 말할 수 없다.

저장소에 버전 릴리스 태그도 없으므로("N개 릴리스 뒤"로 환산할 앵커 없음) 기간은 달력 시간과 관측 조건으로만 정의된다.

**미지수 (사용자만 답할 수 있다)**:

- **N (공지 기간)** — 저장소 밖 소비자에게 통지할 경로와 리드타임이 정해져 있지 않다.
- **W (관측창)** — 소비자별 최장 무호출 간격을 저장소에서 구할 수 없다. 커넥터와 편집기 세션은 사용자 주도라 수 주간 무호출일 수 있다.
- **C (카나리 기간)** — 롤백이 재기동뿐이라는 사실은 **위험의 상한**을 낮출 뿐 기간의 하한을 정하지 않는다. C 는 이상을 며칠 만에 알아채는가(모니터링 주기)와 얼마나 오래 깨져 있어도 되는가(롤백 SLO)에서 도출되며, 최소한 모니터링 주기보다 길어야 한다.

**두 갈래**:

- **안 A (telemetry 선행)** — era 계수를 추가하고 W 만큼 관측해 0 을 확인한 뒤 카나리 C 를 거쳐 제거한다. 제거 근거가 데이터가 된다. 비용은 계측 추가와 재기동이다.
- **안 B (관측 없이 스케줄 컷오버)** — C1 과 C2 를 만족시킨 뒤 공지하고 N 만큼 기다렸다가 modern-only 로 전환한다(C3 생략). 코드 추가가 없는 대신, C2 에서 **"포기"로 분류된** 소비자와 **매트릭스에 없으면서 legacy 표면을 계속 쓰는** 소비자가 컷오버일에 깨진다. 관측이 없으므로 후자의 존재 여부는 알 수 없다.

**계측 도입 여부가 기간 확정보다 먼저 결정되어야 한다** — C4 의 판정 가능성 자체가 거기 걸려 있다.

## 6. C4 는 오늘 판정할 수 없다

**C4 판정에 필요한 범위를 모두 포괄하는 관측 수단이 없다.**

legacy 트래픽의 거부는 서버 게이트 한 곳에서만 일어나지 않는다. **요청이 `handle_request` 에 도달하지 못한 채 종결되는 경로가 여럿 있고**, 그 경로들은 HTTP 계층과 stdio 진입부에 흩어져 있다. legacy-disabled 게이트는 로그를 남기지 않는다. 서버 로그 중 일부가 era 를 구분하기는 한다 — era 분기별로 문구가 다른 것도 있고, 한쪽 분기에만 존재해 발생 자체가 era 를 뜻하는 것도 있다. 그러나 그 로그들은 게이트와 `handle_request` 이전 종결을 포괄하지 못한다. 프록시 쪽은 저장소에 설정이 없어 확인할 수 없고, **저장소에는 프록시에서 legacy 거부를 분리할 수 있는 신호의 설정·조회 근거가 없다**. 따라서 "카나리 기간에 사고가 없었다"를 데이터로 말할 수단이 지금은 없다.

### 재현 (위 상태를 지금 코드에서 확인)

```bash
# (1) HTTP 에서 handle_request 호출보다 앞서 응답이 결정되는 경로가 여럿이다
sed -n '/async def mcp_post/,/async def mcp_get/p' opencrab/mcp/http_app.py \
  | grep -nE 'return (JSONResponse|Response)|server\.handle_request'

# (2) stdio 는 handle_request 에 닿기 전에 먼저 종결되는 경로를 갖는다
sed -n '/def _handle_raw/,/def _dispatch/p' opencrab/mcp/server.py \
  | grep -nE 'json\.loads|return None|return self\._error_response|return self\.handle_request'

# (3a) era 는 두 계층에서 각각 판정된다
grep -n 'is_modern_request' opencrab/mcp/server.py opencrab/mcp/http_app.py

# (3b) 서빙 경로 세 파일 어디에도 계수·메트릭이 없다 (출력이 없어야 한다)
grep -rniE 'metric|counter|telemetry|prometheus|statsd' \
  opencrab/mcp/server.py opencrab/mcp/protocol.py opencrab/mcp/http_app.py

# (3c) legacy-disabled 게이트는 로그를 남기지 않는다 (0 이 나와야 한다)
sed -n '/if not self._enabled_legacy:/,/^        try:/p' opencrab/mcp/server.py \
  | grep -c 'logger\.'

# (3d) 기존 로그 중 era 를 구분하는 것 찾기 (분기별 문구 차이, 한쪽에만 있는 로그)
grep -n 'logger\.' opencrab/mcp/server.py
```

구성별 차이는 계약 테스트 `tests/test_http_app_modern_only.py` 가 고정한다.

### 판정 가능하게 만드는 세 갈래 (사용자 결정)

- (i) **서버에 계측을 추가한다.** 게이트 하나만으로는 부족하다 — 위 재현 (1)(2)가 보이듯 `handle_request` 에 닿지 않고 종결되는 경로가 여럿이다.
- (ii) **엣지에서 계측을 지정한다.** 서버 코드를 그대로 두는 대신 프록시·게이트웨이에서 거부를 분리한다.
- (iii) **계측을 도입하지 않는다.** C4 를 "사용자 문의로 드러난 breakage 없음"이라는 약한 조건으로 낮추고, 그것이 조용히 실패하는 미관측 소비자를 보호하지 못한다는 사실을 명시적으로 수용한다.

**어느 갈래를 고르든 지켜야 하는 안전 조건**:

> 채택한 관측은 **정상 처리되는 legacy 사용**과 **modern-only 전환 시 `handle_request` 이전 종결**을 **모두** 다뤄야 한다. 어느 하나만으로는 거짓 "legacy 0" 이 난다. 그리고 **귀속할 수 없는 트래픽**과 **관측에서 제외한 표면**을 별도로 검토해야 한다.

**갈래별 구조적 사각지대** (갈래를 고르는 데 필요한 비교 근거다):

> **엣지 계측은 stdio 트래픽을 보지 못하고**, **응답 본문만 보는 계측은 빈 바디로 거부되는 요청을 분류하지 못한다**.

어느 갈래든 **관측하지 못하는 표면을 이름으로 적어 두는 것**이 필수다. **"거부 0" 은 관측 범위 안에서의 0 이지 부재의 증명이 아니다.**

(i) 또는 (ii) 를 택하면 함께 확정할 것: **성공 신호**(거부 0 인가 허용 한계 이하인가), **허용 한계**(C2 에서 "포기"로 분류된 소비자의 거부는 신호를 깨지 않는다), **판정 주기**(이 값이 C 의 하한을 정한다).

### 계측 설계는 #263 이 정한다

거부 지점의 전수 도출, 계수 축과 위치, 계수 단위, 지점별 era 귀속 배정은 **#263** 이 정한다. 착수 조건은 위 세 갈래 중 하나를 고르는 것이다. 이 문서가 그것을 대신 확정하지 않는 이유는, 그 서술이 코드가 바뀌면 썩는 좌표이고 정확히 쓰려면 계측 구현과 같은 분석이 필요하기 때문이다.

관측을 채택하면 **"legacy 0" 의 뜻과 계수 방식을 함께 확정해야 한다.** 프로세스 로컬 계수를 채택하면 연속 가동 구간을 기준으로 정의하거나 종료·재기동 전에 값을 외부로 내보내야 한다.

## 7. 롤백 경로

`MCP_PROTOCOL_VERSIONS` 를 미설정으로 되돌리고 재기동한다. 코드 원복은 필요 없다.

**코드가 실제로 삭제된 뒤에는 이 레버가 사라진다.** 따라서 레버가 유효한 modern-only 운영 기간을 거친 뒤에만 삭제한다. 순서를 뒤집으면(삭제 먼저, 관찰 나중) 롤백이 재배포가 된다.

### 재현 (레버가 작동하는지 확인)

```bash
# modern-only 와 dual 구성의 계약 분리
.venv/bin/pytest tests/test_http_app_modern_only.py -q

# 구성 파싱 자체 (미지 버전과 구분자만 있는 값은 기동 거부)
.venv/bin/python -c "
from opencrab.mcp.protocol import parse_enabled_versions as p
print(p('2026-07-28'))          # modern-only
print(p(None))                  # 전체 (기본값)
"
```

## 8. 이 문서가 닫히는 조건

**채택한 경로에서 적용되는 조건**(§4 — 안 A 는 C1~C4, 안 B 는 C1·C2·C4)이 전부 충족되고 제거가 실행되면 이 문서는 제거 PR 의 근거 기록이 되며, `docs/mcp-protocol-compat.md` 에서 legacy 절이 삭제된다. 그 전까지 이 문서는 **미결 상태**다.
