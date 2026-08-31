# MCP 프로토콜 호환성 — 2026-07-28 dual-era 경계 (#136)

LocalCrab MCP 서버가 어떤 프로토콜 세대를 어떻게 서빙하는지, 기존 클라이언트가 무엇을 기대할 수 있는지를 기록한다. 정본 스펙: <https://modelcontextprotocol.io/specification/2026-07-28>.

## 세대(era) 모델

| 세대 | 정의 | 판정 규칙 (요청 단위) |
|---|---|---|
| **modern** | 2026-07-28 이후. 요청마다 `params._meta` 에 버전·capability 를 싣는 stateless 계약 | `method == "server/discover"` 이거나, `params._meta`(dict)에 `io.modelcontextprotocol/protocolVersion` 키가 있음 |
| **legacy** | 2025-11-25 이하. `initialize` handshake 로 세션 의미를 협상 | 위에 해당하지 않는 모든 요청 |

era 의 단일 출처는 **요청 바디**다. HTTP 헤더는 era 판정에 쓰지 않는다 — 2025-06-18 legacy 클라이언트도 `MCP-Protocol-Version` 헤더를 보내기 때문이다. 예외 하나: modern 버전 값(`2026-07-28`)의 헤더가 legacy 바디에 붙으면 스펙 위반 조합으로 400(-32020)에 거부된다.

지원 버전 집합(빌드 내장): modern `2026-07-28`, legacy `2025-11-25 / 2025-06-18 / 2025-03-26 / 2024-11-05`. 현재 값의 기계 확인: `server/discover` 호출(supportedVersions 는 modern 만 나열 — `_meta` 에 legacy 버전을 싣는 것 자체가 모순이므로), 또는 아래 재현 명령.

## modern 경로 계약

- `server/discover`: resultType/supportedVersions/capabilities/instructions/ttlMs/`cacheScope: "public"` + `_meta.serverInfo`.
- `tools/list`: resultType, ttlMs, **`cacheScope: "private"`** (#150 — 목록이 principal 별로 달라 공유 캐시 금지), 결정적 순서(레지스트리 `order=` 고정), 단일 페이지(`nextCursor` 없음, 임의 cursor 제시는 -32602).
- `tools/call`: resultType, `isError` (도구가 예외를 던진 경우와 예외 없이 최상위 `{"error": ...}` 를 반환한 경우 모두 true). 미지 도구는 **-32602** (legacy 는 종전대로 -32601).
- `ping`·`initialize` 는 modern 에서 제거된 메서드 → -32601.
- 오류: 미지원 버전 → **-32022** + `data.supported/requested`, `_meta` 필수 필드 누락·malformed → -32602.
- 모든 modern result 에 `_meta.io.modelcontextprotocol/serverInfo` 를 싣는다(오류 응답에는 싣지 않음).

### Streamable HTTP 전송 (modern 단일 요청)

- 표준 헤더 필수: `MCP-Protocol-Version`(바디 `_meta` 값과 일치), `Mcp-Method`(바디 method 와 일치), `tools/call` 은 `Mcp-Name`(바디 `params.name` 과 일치, `=?base64?…?=` sentinel 디코딩 지원). 위반 → 400 + **-32020**. **헤더 검증이 바디 검증보다 먼저다**: `_meta` 없는 `server/discover` 는 HTTP 에서 -32020, stdio(헤더 계층 없음)에서 -32602.
- 상태 매핑: 미지 메서드 -32601 → **404**, 검증 계열(-32700/-32600/-32602/-32020/-32021/-32022) → **400**, 그 외(-32603 포함)는 종전대로 200 (스펙 무규정 — 최소 이탈).
- JSON-RPC **배치 배열은 legacy 전용 LocalCrab 확장**이다 (2026-07-28 은 POST 당 요청 1건). modern 표식(원소의 modern `_meta`·`server/discover`·비-dict `_meta`, 또는 modern 버전 헤더)이 있는 배열은 400 + -32600.
- `GET /mcp`·`DELETE /mcp` → **405** (`Allow: POST`). 2026-07-28 규정이며 legacy Streamable HTTP 도 세션 미발급 서버의 405 를 허용한다. (변경 전 DELETE 는 200 ack 였다.)
- **Origin 검증**(모든 `/mcp` 요청, 라우팅·인증보다 앞): Origin 헤더 부재 → 통과(비브라우저 클라이언트). loopback(`localhost`/`127.0.0.1`/`[::1]`, 임의 포트) → 통과. `MCP_ALLOWED_ORIGINS` 정확 일치 → 통과. 그 외 → **403**(no-store). DNS rebinding 방어의 스펙 MUST.

## legacy 경로 계약 (호환 계층)

`initialize`/`notifications/initialized` handshake, `ping`, `_meta` 없는 `tools/list`·`tools/call`, JSON-RPC 배치 전부 종전 그대로 동작한다. 응답 봉투도 바이트 호환이다(modern 필드를 섞지 않는다). 변경은 정확히 하나: **initialize 가 미지 버전을 무검증 echo 하지 않는다.** 지원 legacy 버전은 그대로 echo, 버전 부재는 종전 fallback `2024-11-05`, 미지·modern 전용 버전은 서버가 서빙하는 최신 legacy(`2025-11-25`)를 제시하고 클라이언트가 진행 여부를 판단한다(legacy 협상 규칙).

## 클라이언트 호환성 매트릭스

| 소비자 | 방식 | #136 이후 |
|---|---|---|
| claude.ai 커넥터 (cloudflared 경유) | legacy initialize handshake (2025-03-26/06-18) + 쿼리 토큰 | 무변경 (지원 legacy 버전 echo 유지) |
| Claude Code / Claude Desktop / Cursor | legacy handshake, 일부 `MCP-Protocol-Version` legacy 헤더 동반 | 무변경 (legacy 헤더는 검증 대상 아님) |
| 대화 reingest hook `lc_call` (hooks bundle) | initialize 없이 legacy `tools/call` POST, `_meta`·MCP 헤더 없음 | 무변경 |
| 로더 계획(ingestion-via-mcp-plan)의 JSON-RPC 배치 | legacy 배치 배열 | 무변경 (legacy 전용 확장으로 유지) |
| modern (2026-07-28) 클라이언트 | 요청별 `_meta` + 표준 헤더, 필요 시 `server/discover` probe | 신규 지원 |
| 브라우저 직접 호출 (Origin 헤더 동반) | — | loopback 외에는 `MCP_ALLOWED_ORIGINS` 등록 필요, 미등록 403 |

## 설정

| env | 의미 | 오류 시 |
|---|---|---|
| `MCP_PROTOCOL_VERSIONS` | 쉼표 구분 부분집합으로 지원 버전 제한 (미설정 = 전체). legacy 전부 제외 = handshake 비활성(D절 이행 레버), modern 전부 제외 = legacy 고정 | 빌드가 모르는 버전이 있으면 **기동 거부** |
| `MCP_ALLOWED_ORIGINS` | 쉼표 구분 Origin allowlist. `http(s)://host[:port]` 형태만, 브라우저가 보내는 형태 그대로(기본 포트 생략) | 경로·쿼리·userinfo 포함, `null`, 비-http 스킴이면 **기동 거부** |

두 설정 모두 standalone(`opencrab serve`)과 apps/api 양쪽에서 서빙 시작 전에 검증된다. 두 설정 모두 **완전히 빈 값(미설정·빈 문자열)은 기본값**이지만, **구분자·빈 항목만 있는 값(예: `,`, `a,,b`)은 malformed 로 기동 거부**된다 — 오타로 항목이 사라진 채 조용히 뜨는 것을 막는다.

**프로덕션 재기동 주의**: 알려진 소비자는 전부 Origin 헤더를 보내지 않아 기본값으로 안전하다. Origin 을 싣는 새 소비자가 403 을 받으면 코드 원복 없이 `MCP_ALLOWED_ORIGINS` 에 그 Origin 을 추가하고 재기동하면 된다.

## 범위 밖 (후속)

- **`subscriptions/listen` / `listChanged`**: 도구 레지스트리가 import 시점에 고정되어 프로세스 수명 동안 목록이 변하지 않으므로, capability 를 선언하지 않는 것이 진실한 선언이다. 목록이 런타임에 변하게 되면 그때 #150 의 principal 별 통지 문제와 함께 설계한다. #135 가 가시 뷰 기준 카탈로그 fingerprint(`tool_search` 의 `catalog_version`)를 도입했지만 목록은 여전히 불변이므로 이 결정은 유지된다(docs/mcp-tool-catalog.md).
- **MRTR / tasks extension**: 서버발 요청(roots/sampling/elicitation)이 코드베이스에 없어 적용 대상이 없다.
- **structuredContent**: 소비자가 생기면 추가한다.
- **D절 제거(ping·legacy handshake 삭제)**: deprecation 기간 이후 별도 결정. 이행 레버는 `MCP_PROTOCOL_VERSIONS`(legacy 제외 구성)로 이미 존재한다.
- **HTTP+SSE (2024-11-05 구 전송)**: 종전부터 미구현이며 새로 추가하지 않는다.

## 재현 명령 (살아 있는 상태 확인)

```bash
# 계약 테스트 전체
.venv/bin/pytest tests/test_mcp_protocol_2026.py tests/test_http_app_modern.py -q

# discover 를 stdio 계층에서 직접 확인 (테스트 서버 불필요)
.venv/bin/python - <<'EOF'
from unittest.mock import MagicMock, patch
with patch("opencrab.mcp.server.get_settings") as cfg:
    cfg.return_value = MagicMock(mcp_server_name="x", mcp_server_version="0", mcp_protocol_versions=None)
    from opencrab.mcp.server import MCPServer
    print(MCPServer().handle_request({"jsonrpc": "2.0", "id": 1, "method": "server/discover",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                              "io.modelcontextprotocol/clientCapabilities": {}}}}))
EOF
```
