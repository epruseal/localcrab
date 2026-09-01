# LocalCrab Agent Plugin 호환성 (compatibility matrix)

이 문서는 LocalCrab Agent Plugin 의 클라이언트 호환성 정본이다. 릴리스 세트의
`localcrab-plugin-<v>.COMPATIBILITY.md` 는 이 문서에서 결정론적으로 생성된다
(빌드 절차와 산출물 설계는
https://github.com/epruseal/localcrab/blob/main/docs/agent-plugin-packaging.md
참고). 이 문서는 아카이브와 별도로 단독 배포되므로 저장소 상대 링크를 두지
않는다.

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
