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

정본은 [`docs/agent-plugin-compatibility.md`](./agent-plugin-compatibility.md)
로 이동했다. 클라이언트별 설치 방식·stdio/streamable-http·MCP protocol·auth·
PLUGIN_ROOT/PLUGIN_DATA 지원 매트릭스와 근거, Claude Code 수동 매핑, Streamable
HTTP entry 를 출하하지 않는 사유는 그 문서를 본다. 릴리스 세트에 동봉되는
`localcrab-plugin-<v>.COMPATIBILITY.md` 는 이 정본 문서에서 생성된다(아래
`## 릴리스 산출물과 검증` 참고).

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

1. **설치** — 두 플래그가 **모두** 필요하다. `--force` 는 ClawHub 외부 로컬
   경로라서, `--accept-capabilities` 는 MCP 를 선언한 번들이라서 요구된다.
   후자를 빠뜨리면 설치는 진행되지만 그 뒤 **CLI 기동 자체가 막힌다**
   (`requires capability consent`).

   ```bash
   openclaw plugins install --force --accept-capabilities <dist>/localcrab-plugin
   ```

2. **발견** — `openclaw plugins inspect <plugin.json 의 name>` 출력에
   `Bundle format: agent (Agent Plugins)`, `Bundle capabilities` 의
   `mcpServers`, 그리고 `MCP servers:` 목록의 서버 이름이 모두 있어야 한다.
   조회 식별자는 설치 디렉터리명이 아니라 `plugin.json` 의 `name` 이다.
3. **프로비저닝** — 자동 경로(기본): mcp.json 이 실어 보내는
   `OPENCRAB_BOOTSTRAP_ON_EMPTY=1` 그대로, **미리 만들어 둔 빈** `PLUGIN_DATA`
   에서 최초 stdio 기동만 실행해 `<PLUGIN_DATA>/opencrab.db` 실재를 확인한다
   (수동 init 없이 기동만으로 성립). 두 가지를 주의한다. **디렉터리는 미리
   있어야 한다** — 서버는 자기 데이터 디렉터리를 만들지 않고, 클라이언트도 최초
   기동 전에는 만들어 두지 않아 없으면 기동이 실패한다(실측). 그리고 **서버가
   stderr 로 내는 생성 공지는 판정 근거로 쓰지 않는다** — 클라이언트가 MCP 서버
   자식의 stderr 를 호출자에게 넘겨주지 않아 관측할 수 없다(실측). 관측 가능한
   증거는 생성된 파일이다. 수동 경로도 병기 검증한다: 위 정본 `opencrab init`
   명령을 별도의 빈 `PLUGIN_DATA` 에서 실행해 같은 `opencrab.db` 실재로 대체
   경로가 여전히 유효함을 확인한다
4. **실행+도구 노출** — **embedded agent turn 으로만 확인한다.**
   `mcp list`/`mcp probe` 는 쓰지 않는다: 두 명령은 설정 파일의 관리형
   `mcp.servers` 항목만 읽으므로, 번들이 공급한 MCP 서버는 감지·기동이 모두
   성공한 상태에서도 "configured 된 서버가 없다"로 나온다. 이것을 실패 신호로
   읽으면 오진한다.
5. **tools/call** — 아래 네 조건을 **모두** 만족했을 때만 충족으로 기록한다.
   도구 노출 확인(4단계)만으로는 호출 충족으로 기록하지 않는다.

   1. 클라이언트와 서버 사이 stdio 경계에서 JSON-RPC **원문**을 관측한다.
   2. 실행별 난수를 인자로 받는 **변경 연산**을 최소 1회 호출한다. 정적 무인자
      도구만 호출하면 "결과를 합성해 넣었다"는 반례를 배제하지 못한다 — 결과가
      프로세스 간 결정론적이라 어디서 온 값인지 구별되지 않기 때문이다.
   3. 그 난수가 provider 요청의 assistant tool call 인자, 경계 원문의
      `tools/call.params.arguments`, 서버 응답, provider 가 되받은 `role=tool`
      메시지에 **연속으로** 나타난다.
   4. turn 종료 후 **별도 프로세스**가 같은 스토어에 새 stdio 세션을 열어 그
      변경의 부작용이 실재함을 확인한다.

   **두 모드를 모두 돌린다.** 각각이 증명하는 것이 다르고 서로를 대체하지
   않는다.

   - 기록기 활성: 클라이언트가 실제 `tools/call` 프레임을 보냈음을 보인다.
     다만 기록기가 기동 경로에 있으므로, 그 코드를 신뢰하지 않는 사람에게는
     이것만으로 닫히지 않는다.
   - 기록기 비활성: 기동 경로에 하네스 코드가 없는 상태에서 부작용이 생겼음을
     보인다. 클라이언트가 상태를 바꿨다는 것까지만 증명하고, 그 수단이 MCP
     였다는 것은 증명하지 않는다 — 클라이언트가 MCP 아닌 경로로 같은 스토어를
     바꿨을 가능성이 남는다. 그 가능성은 앞의 모드가 닫는다.

   기록기를 태웠다고 해놓고 경계 원문이 비면 **실패로 기록한다.** 통과로 처리하면
   기록기가 개입하지 못한 실행이 "기록기를 뺀 실행"으로 조용히 강등된다.

**함정: 경계에 래퍼를 끼울 때.** 클라이언트는 MCP 서버 자식 프로세스에 환경을
**소독해서** 넘긴다 — 자식이 받는 변수는 극소수다. 따라서 경계 기록기가 자기
설정(실제 바이너리 경로 등)을 환경 변수로 받으려 하면 자식에서 즉사하고, 증상은
`failed to start server ... Connection closed` 라는 **서버 기동 실패로만** 보인다.
기록기는 경로를 소스에 상수로 박아야 한다. 또 기록기는 아무것도 파싱하지 않는
바이트 tee 여야 한다 — 프레임을 해석하면 그 기록은 독립 관측이 아니게 된다.

### 재현 수단

위 사다리는 러너로 실행할 수 있다. 수작업으로 하면 재현 수단이 남지 않고,
그러면 다음 사람이 도달 지점을 대사할 수 없다.

```bash
python scripts/verify_openclaw_e2e.py \
    --plugin-dist <dist>/localcrab-plugin \
    --opencrab-bin "$(command -v opencrab)" \
    --client-bin "$(command -v openclaw)" \
    --scratch <스크래치 루트>
```

러너는 격리 계약(스크래치 `HOME`, 스크래치 CWD, 허용목록 환경, 임시 루프백
포트)을 스스로 지킨다. 수행 범위는 **1·2·4·5단계와 3단계의 자동 경로**다:
설치, 발견, 빈 데이터 루트 확인, embedded turn, 자동 부트스트랩으로
`opencrab.db` 가 생겼는지 확인, 사후 독립 조회, 증거 판정.

**러너가 하지 않는 것 하나.** 3단계의 수동 `opencrab init` 병기 검증은 하지
않는다 — 그 경로는 위 정본 명령으로 별도의 빈 데이터 루트에서 직접 확인한다.
빈 데이터 루트를 준비하는 것은 3단계가 적었듯 러너 몫이며, 러너가 관측하는 것은
**그 안에 스토어가 수동 init 없이 생기는가**다.

러너는 사전 조회로 스토어를 건드리지 않는다. 조회 자체가 부트스트랩을
일으켜 3단계의 관측 대상을 없애기 때문이다. 대신 데이터 루트가 비었음을
확인한다 — 스토어가 없으면 난수 노드도 있을 수 없으므로 부재 확인으로 충분하다.

`--no-recorder` 를 주면 경계 기록기 없이 돈다. 위에 적었듯 두 모드를 모두
돌려야 결론이 닫힌다. 증거 판정기는 `tests/test_openclaw_e2e_evidence.py` 가
실제 실행에서 캡처한 픽스처와 역변이로 회귀를 잡는다(CI 에서 돈다). 픽스처에
적용한 편집은 `tests/fixtures/openclaw_e2e/make_fixtures.py` 가 정본이다. 러너
자체는 실 클라이언트 설치를 요구하므로 CI 게이트가 아니다.

모델 자격증명 부재로 agent turn 자체가 성립하지 않으면(새 자격증명 발급이나
비용 발생은 하지 않는다), 도달한 단까지만 **부분 충족**으로 명시하고 어느
단에서 멈췄는지를 보고에 남긴다. 실측 도달 여부와 타임스탬프는 이 문서가
아니라 PR·이슈 보고에 남긴다 — 이 문서에는 절차와 판정 기준만 둔다.

## 릴리스 산출물과 검증

빌드는 `dist/`(out_dir) 아래 릴리스 세트를 만든다:

```
dist/
├── localcrab-plugin/                          # 스테이징 디렉터리 -- 로컬 편의 산출물, 릴리스 첨부 대상 아님
├── localcrab-plugin.SHA256SUMS                # 패키지 파일별 해시(사이드카)
├── localcrab-plugin-<v>.tar.gz                # 릴리스 아카이브(결정론, top prefix localcrab-plugin/)
├── localcrab-plugin-<v>.COMPATIBILITY.md      # compat report -- docs/agent-plugin-compatibility.md 에서 생성
└── localcrab-plugin-<v>.RELEASE.SHA256SUMS    # 릴리스 세트 해시(tar.gz, COMPATIBILITY.md, 패키지 SHA256SUMS 3항목)
```

`<v>` 는 pyproject `[project].version`. `localcrab-plugin/` 스테이징 디렉터리는
파일을 직접 들여다보기 위한 로컬 편의 산출물이며 GitHub Release 첨부 대상이
아니다 — 첨부하는 것은 위 4개 파일(staged 디렉터리 제외)뿐이다.

### 수령자 검증

```bash
sha256sum -c localcrab-plugin-<v>.RELEASE.SHA256SUMS
python scripts/build_agent_plugin.py --verify --out dist
```

앞 명령은 릴리스 세트 3파일의 해시를 다운로드본과 대사한다. 뒤 명령은 그에
더해 아카이브 멤버와 패키지 사이드카를 상호 대사해 세트 내부 일관성까지
확인한다.

### 재현성 성질과 범위

동일 POSIX 도구체계(같은 CPython 계열 + 번들 zlib)와 LF checkout 에서 빌드하면
tar.gz·COMPATIBILITY.md·RELEASE.SHA256SUMS 는 바이트 단위로 동일하다. zlib
구현이 다른 환경 사이에서 gz 바이트가 동일함은 주장하지 않는다 — 권위 해시는
항상 릴리스를 실제로 빌드한 산출값이다.

재현 확인 명령:

```bash
python scripts/build_agent_plugin.py --out /tmp/repro-a
python scripts/build_agent_plugin.py --out /tmp/repro-b
sha256sum /tmp/repro-a/localcrab-plugin-*.tar.gz /tmp/repro-b/localcrab-plugin-*.tar.gz
```

운영 정책(공표 위치, 진본성 경계, 수동 릴리스 절차)은
[`docs/agent-plugin-release-policy.md`](./agent-plugin-release-policy.md) 참고.

## 비범위

registry/marketplace/sandbox/permission UI, 개인 데이터, client extension
승격, Claude Code 자동 변환 어댑터, `opencrab/mcp/**` 수정, `ci.yml`/
`pyproject.toml` 수정, 라이브 자원 접촉은 이 이슈의 범위 밖이다.
