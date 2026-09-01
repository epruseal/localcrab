# Agent Plugin 패키징 (이슈 #137)

## 목적

LocalCrab 을 Agent Plugins 1.0.0 표준으로 패키징한다. 이 패키지가 공급하는 것은
**discovery 와 설정**뿐이다 — 클라이언트가 플러그인을 찾아 `mcp.json` 대로
`opencrab serve` 를 stdio 서브프로세스로 기동하게 하는 매니페스트와 skill 문서다.
**런타임 자체는 공급하지 않는다.** `opencrab` 은 `pip install` 로 별도 설치돼
있어야 하고, 실행 파일이 클라이언트가 서브프로세스를 띄우는 PATH 위에 있어야 한다.

## 재현 명령

빌드:

```bash
python scripts/build_agent_plugin.py
```

검증(패키징 테스트 + 스모크):

```bash
pytest tests/test_agent_plugin_packaging.py tests/test_agent_plugin_smoke.py
```

프로비저닝(패키지 README 정본 절차, `<DATA>` 는 클라이언트가 만든 `PLUGIN_DATA`
경로) — 자동/수동 두 경로가 있다:

**자동(기본 경로)**: 이 패키지의 `mcp.json` 은 `OPENCRAB_BOOTSTRAP_ON_EMPTY=1`
을 함께 실어 보낸다. 빈 `<DATA>` 에서 mcp.json env 그대로 최초 stdio 기동만
하면 로컬 유저와 빈 스토어가 자체 생성된다. 게이트(opt-in="1", STORAGE_MODE=local,
LOCAL_DATA_DIR 명시·비공백·`?` 미포함, `<DATA>` 디렉터리 실재) 중 하나라도
위반하면 조용한 폴백 대신 전용 오류로 기동을 거부한다. 생성 시 stderr 로
경로·user_id 1줄 공지가 남는다.

**수동(대체·복구 경로)**: 자동 부트스트랩을 껐거나 위 게이트를 만족시킬 수
없는 문맥, 또는 HTTP 용 토큰이 필요한 경우(자동 경로는 토큰을 발급하지
않는다 — 발급하려면 `opencrab token issue <user_id>`) 여전히 아래로 직접
프로비저닝한다:

```bash
cd <DATA>
STORAGE_MODE=local LOCAL_DATA_DIR=<DATA> LOCALCRAB_ENV_FILE=<DATA>/localcrab.env opencrab init
```

첫 기동이 `Run 'opencrab init' first` 로 실패하면 이번 기동에서 자동
부트스트랩이 작동하지 않았다는 뜻이다(opt-in 이 꺼져 있거나 위 게이트 중
하나를 위반) — 오류문 자체가 해석된 데이터 루트 경로와
`OPENCRAB_BOOTSTRAP_ON_EMPTY=1` opt-in 안내를 함께 낸다. 클라이언트가 표시하는
`PLUGIN_DATA` 경로에서 위 수동 명령을 실행한 뒤 재시도한다 — 이는 결함이
아니라 미프로비저닝(또는 opt-in 미충족) 데이터 디렉터리의 정상 실패 경로다.

## Compatibility matrix

행은 확인 대상 클라이언트, 열은 지원 축이다.

| | 설치 방식 | stdio | streamable-http | MCP protocol | auth | PLUGIN_ROOT/PLUGIN_DATA |
|---|---|---|---|---|---|---|
| **OpenClaw** (2026.8.1 이상) | 로컬 디렉터리 install, Agent Plugins 번들 네이티브 감지(`Bundle format: agent (Agent Plugins)`). 2026.7.x 이하는 Agent Plugins 감지 이전이라 `claude` 로 오분류되어 skills 만 매핑된다(실측) | 지원(공식 문서 근거) | 공식 문서상 프로토콜 자체는 지원되나 이 패키지는 stdio entry 만 출하(아래 사유 참고) | legacy initialize echo(2024-11-05/2025-03-26). 이슈 #136 에서 2026-07-28 협상 방식 전환 예정 | stdio=local principal(#145 근거), http=per-user bearer(패키지 비포함 — 오퍼레이터 문서) | 지원(공식) — 상태 디렉터리 아래 영속 per-plugin 디렉터리를 만들고 args/env/cwd 에 단일 패스로 치환 |
| **Claude Code** | 1.0.0 매니페스트 네이티브 비호환 — 수동 매핑 필요(아래) | 자체 포맷(`.mcp.json`)으로 재작성해야 동작 | 자체 포맷으로 재작성해야 동작 | 해당 없음(독립 클라이언트 구현) | 해당 없음(이 이슈 범위 밖) | 미지원 — 자체 `${CLAUDE_PLUGIN_ROOT}`/`${CLAUDE_PLUGIN_DATA}` |
| **레퍼런스 클라이언트(스모크)** | 해당 없음 — CI 전용 로더(`tools/refclient.py`) | 지원(테스트 대상 경로) | 미구현(entry 를 출하하지 않으므로 대상 아님) | legacy initialize echo(스모크 기준) | 해당 없음(principal 개념 없음) | 지원(테스트가 tmp 경로로 직접 치환) |

근거: OpenClaw 는 공식 문서(docs.openclaw.ai/plugins/bundles)가 로컬 디렉터리
install, `Bundle format: agent (Agent Plugins)` 감지, stdio MCP 서버 기동,
`PLUGIN_ROOT`/`PLUGIN_DATA` env 계약과 placeholder 확장을 명시한다. Claude Code
는 자체 포맷(`.claude-plugin/plugin.json`, `${CLAUDE_PLUGIN_ROOT}`)만 읽으므로
1.0.0 매니페스트를 그대로 이해하지 못한다.

### Claude Code 수동 매핑

Claude Code 로 이식하려면 아래를 손으로 옮겨 적어야 한다(자동 변환 어댑터는
이 이슈의 비범위 — client extension 승격 금지에 저촉되지 않는 외부 도구로만
후속 검토):

| Agent Plugins 1.0.0 | Claude Code |
|---|---|
| 루트 `plugin.json` | `.claude-plugin/plugin.json` |
| `mcp.json` | `.mcp.json` |
| `${PLUGIN_ROOT}` | `${CLAUDE_PLUGIN_ROOT}` |
| `${PLUGIN_DATA}` | `${CLAUDE_PLUGIN_DATA}` |

### Streamable HTTP entry 를 출하하지 않는 이유

`mcp.json` 에는 stdio entry 만 있다. HTTP entry 를 넣으려면 운영 endpoint
(host:port)와 인증 토큰이 필요한데 둘 다 오퍼레이터 고유값이라 portable 패키지
계약에 담을 수 없다 — 스펙상 non-loopback endpoint 는 HTTPS 와 authorization
을 요구하며, 이는 패키지가 미리 알 수 없는 값이다. 원격 접근 설정은 저장소
README와 `docs/mcp-client-auth.md` 의 오퍼레이터 절차로 남긴다. legacy `sse`
transport 는 채택하지 않는다 — 1.0.0 표준은 streamable-http 를 정본으로 두고
`sse` 는 레거시 호환용이라 신규 패키지에 넣을 이유가 없다.

## 환경 변수

정본은 `packaging/agent-plugin/tools/env_contract.py` 의 `ENV_CONTRACT` 다.
이 정본은 각 환경변수 이름을 분류(설정 소스 선택/외부 전송 결정/상태 위치/
기동 거부/튜너블)와 `opencrab serve`(stdio) 도달 여부까지 코드로 들고 있고,
`tests/test_agent_plugin_packaging.py` 의 AST 기반 가드가 `opencrab/`·`apps/`
전수 스캔 결과(Settings alias + 직접 읽기 + 명시적 indirect 목록)와 이 정본이
**양방향 동치**임을 강제한다. 문서에만 있거나 코드에만 있는 이름은 가드
실패로 드러난다 — 새 환경변수가 코드에 추가되면 정본을 갱신할 때까지 이
가드가 실패하는 것이 의도된 성질이다.

재현 명령:

```bash
pytest tests/test_agent_plugin_packaging.py -k env_contract -q
```

코드 쪽 정본을 직접 보려면:

```bash
cat packaging/agent-plugin/tools/env_contract.py
```

### 보안 호출아웃

`ENV_CONTRACT` 분류 중 특히 주의할 것:

- **외부 전송 결정**: `POSTGRES_URL`, `NEO4J_URI`/`NEO4J_USER`/`NEO4J_PASSWORD`,
  `MONGODB_URI`, `CHROMA_HOST`/`CHROMA_PORT`, `OPENAI_API_BASE`,
  `OPENAI_API_KEY`, `EMBEDDING_BACKEND` — 값에 따라 서버가 어디로 나가는지가
  바뀐다.
- **상태 위치**: `LOCAL_DATA_DIR`, `VECTOR_DB_FILE`(절대경로 지정 시
  `LOCAL_DATA_DIR` 무시), `LOCAL_GGUF_PATH` — 데이터가 실제로 어디 쓰이는지가
  바뀐다.
- **기동 거부(안전 실패)**: `OPENCRAB_API_KEY`, `LOCALCRAB_MCP_TOKEN`,
  `LOCALCRAB_MCP_TOKEN_FILE` — 남아 있으면 서버가 기동을 거부한다. 폐기된
  공유 비밀 인증 방식의 안전 실패이며 결함이 아니다.
- **상태 생성 opt-in**: `OPENCRAB_BOOTSTRAP_ON_EMPTY` — 값이 "1"이면 빈
  데이터 루트에 로컬 유저와 빈 스토어를 생성한다(#245). unset/""/"0" 은
  off(기존 동작 불변)이고, 그 외 malformed 값은 기동을 거부한다. 이 패키지의
  `mcp.json` 이 이 값을 "1"로 명시 공급한다.

이 패키지의 `mcp.json` 은 위 목록 중 **외부 전송 결정**·**기동 거부** 항목은
어느 것도 설정하지 않는다. **상태 위치**(`LOCAL_DATA_DIR`)와 **상태 생성
opt-in**(`OPENCRAB_BOOTSTRAP_ON_EMPTY`)은 이 패키지가 의도적으로 설정하는
4키(`STORAGE_MODE`/`LOCAL_DATA_DIR`/`LOCALCRAB_ENV_FILE`/
`OPENCRAB_BOOTSTRAP_ON_EMPTY`)에 포함되며, 그 값과 게이트는 위 항목 설명대로다.
서버가 실제로 어디로 나가고 무엇을 거부하는지는 **클라이언트가 서브프로세스에
상속시키는 ambient 환경**에 달려 있다. 스펙의 base-env 무의존 의무는 이 4키의
명시 공급으로 충족되지만, 클라이언트가 ambient env 를 상속하기로 선택하는
것 자체는 스펙상 client-defined 라 패키지가 통제할 수 없다. 권고: 이 플러그인을
기동하는 프로세스의 ambient 환경을 sanitize 하거나, 위 목록의 외부 전송·기동
거부 변수가 의도치 않게 설정돼 있지 않은지 확인한다.

## 벤더링 스키마 무결성

`packaging/agent-plugin/schemas/{plugin,mcp}.schema.json` 은
https://agent-plugins.org/schemas/1.0.0/ 의 오프라인 사본이다. 값을 문서에
박지 않고 아래 명령으로 그때그때 확인한다.

```bash
sha256sum packaging/agent-plugin/schemas/*.json
```

## 실 클라이언트 검증

CI 게이트는 항상 레퍼런스 클라이언트 스모크다(`tests/test_agent_plugin_smoke.py`)
— 로컬 openclaw 설치는 CI 환경에 없다. 실 클라이언트(OpenClaw) 검증은 **로컬에서
1회**, 격리 계약(scratch `HOME` + scratch `CWD` + 명시 임시 포트, `--profile` 은
라이브 상태를 마이그레이션하므로 금지) 아래 사다리로 도달 지점을 기록한다:

1. **설치** — `openclaw plugins install <dist>/localcrab-plugin`
2. **발견** — `openclaw plugins list`/`inspect` 로 `Bundle format: agent
   (Agent Plugins)` 및 MCP 서버·skill 감지 확인
3. **프로비저닝** — 자동 경로(기본): mcp.json 이 실어 보내는
   `OPENCRAB_BOOTSTRAP_ON_EMPTY=1` 그대로, OpenClaw 가 만든 빈 `PLUGIN_DATA`
   에서 최초 stdio 기동만 실행해 `<PLUGIN_DATA>/opencrab.db` 실재와 stderr
   생성 공지를 확인한다(수동 init 없이 기동만으로 성립). 수동 경로도 병기
   검증한다: 위 정본 `opencrab init` 명령을 별도의 빈 `PLUGIN_DATA` 에서
   실행해 같은 `opencrab.db` 실재로 대체 경로가 여전히 유효함을 확인한다
4. **실행+도구 노출** — `mcp probe`/`mcp list` 또는 embedded agent turn 에서
   도구 목록 확인
5. **tools/call** — embedded agent turn 에서 `ontology_manifest` 를 **명시
   호출**한 사건과, 그 호출에 대응하는 결과에서 `spaces`/`meta_edges` 키
   실재를 전사(또는 원시 MCP 로그)로 확인했을 때만 충족으로 기록한다. 도구
   노출 확인(4단계)만으로는 호출 충족으로 기록하지 않는다.

모델 자격증명 부재로 agent turn 자체가 성립하지 않으면(새 자격증명 발급이나
비용 발생은 하지 않는다), 도달한 단까지만 **부분 충족**으로 명시하고 어느
단에서 멈췄는지를 보고에 남긴다. 실측 도달 여부와 타임스탬프는 이 문서가
아니라 PR·이슈 보고에 남긴다 — 이 문서에는 절차와 판정 기준만 둔다.

## 비범위

registry/marketplace/sandbox/permission UI, 개인 데이터, client extension
승격, Claude Code 자동 변환 어댑터, `opencrab/mcp/**` 수정, `ci.yml`/
`pyproject.toml` 수정, 라이브 자원 접촉은 이 이슈의 범위 밖이다.
