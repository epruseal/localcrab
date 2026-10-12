# MCP 프로토콜 호환성 — 2026-07-28 dual-era 경계 (#136)

LocalCrab MCP 서버가 어떤 프로토콜 세대를 어떻게 서빙하는지, 기존 클라이언트가 무엇을 기대할 수 있는지를 기록한다. 정본 스펙: <https://modelcontextprotocol.io/specification/2026-07-28>.

## 세대(era) 모델

| 세대 | 정의 | 판정 규칙 (요청 단위) |
|---|---|---|
| **modern** | 2026-07-28 이후. 요청마다 `params._meta` 에 버전·capability 를 싣는 stateless 계약 | `method == "server/discover"` 이거나, `params._meta` 가 present 이고 **비-dict**(malformed 표식이므로 modern 으로 보내 거부)이거나, `params._meta`(dict)에 `io.modelcontextprotocol/protocolVersion` 키가 있음 |
| **legacy** | 2025-11-25 이하. `initialize` handshake 로 세션 의미를 협상 | 위에 해당하지 않는 모든 요청 |

era 의 단일 출처는 **요청 바디**다. HTTP 헤더는 era 판정에 쓰지 않는다 — 2025-06-18 legacy 클라이언트도 `MCP-Protocol-Version` 헤더를 보내기 때문이다. 예외 하나: modern 버전 값(`2026-07-28`)의 헤더가 legacy 바디에 붙으면 스펙 위반 조합으로 400(-32020)에 거부된다.

지원 버전 집합(빌드 내장): modern `2026-07-28`, legacy `2025-11-25 / 2025-06-18 / 2025-03-26 / 2024-11-05`. 현재 값의 기계 확인: `server/discover` 호출(supportedVersions 는 modern 만 나열 — `_meta` 에 legacy 버전을 싣는 것 자체가 모순이므로), 또는 아래 재현 명령.

## modern 경로 계약

- `server/discover`: resultType/supportedVersions/capabilities/instructions/ttlMs/`cacheScope: "public"` + `_meta.serverInfo`.
- `tools/list`: resultType, ttlMs, **`cacheScope: "private"`** (#150 — 목록이 principal 별로 달라 공유 캐시 금지), 결정적 순서(레지스트리 `order=` 고정), 단일 페이지(`nextCursor` 없음, 임의 cursor 제시는 -32602).
- `tools/call`: resultType, `isError` (도구가 예외를 던진 경우와 예외 없이 최상위 `{"error": ...}` 를 반환한 경우, 또는 `pack_ingest`가 `status: "partial"`을 반환한 경우 true). 미지 도구는 **-32602** (legacy 는 종전대로 -32601).
- `ping`·`initialize` 는 modern 에서 제거된 메서드 → -32601.
- 오류: 미지원 버전 → **-32022** + `data.supported/requested`, `_meta` 필수 필드 누락·malformed → -32602.
- 모든 modern result 에 `_meta.io.modelcontextprotocol/serverInfo` 를 싣는다(오류 응답에는 싣지 않음).

### Streamable HTTP 전송 (modern 단일 요청)

- 표준 헤더 필수: `MCP-Protocol-Version`(바디 `_meta` 값과 일치), `Mcp-Method`(바디 method 와 일치), `tools/call` 은 `Mcp-Name`(바디 `params.name` 과 일치, `=?base64?…?=` sentinel 디코딩 지원). 위반 → 400 + **-32020**. **헤더 검증이 바디 검증보다 먼저다**: `_meta` 없는 `server/discover` 는 HTTP 에서 -32020, stdio(헤더 계층 없음)에서 -32602.
- 상태 매핑: 미지 메서드 -32601 → **404**, 검증 계열(-32700/-32600/-32602/-32020/-32021/-32022) → **400**, 그 외(-32603 포함)는 종전대로 200 (스펙 무규정 — 최소 이탈). **legacy 비활성(modern-only) 구성에서는 이 매핑이 모든 단일 요청에 적용된다(#250)**: legacy 형상 요청의 거부도 400 이고, 오류 봉투는 이 status 정합 수정 전후로 불변이다(같은 구성에서 HTTP status 만 바뀐다). 같은 구성에서 배열 바디(빈 배열 포함)와 비객체 바디는 400 + -32600 단일 오류 객체로, legacy 형상 알림은 400 빈 바디로 거부된다.
- JSON-RPC **배치 배열은 legacy 전용 LocalCrab 확장**이다 (2026-07-28 은 POST 당 요청 1건). modern 표식(원소의 modern `_meta`·`server/discover`·비-dict `_meta`, 또는 modern 버전 헤더)이 있는 배열은 400 + -32600.
- `GET /mcp`·`DELETE /mcp` → **405** (`Allow: POST`). 2026-07-28 규정이며 legacy Streamable HTTP 도 세션 미발급 서버의 405 를 허용한다. (변경 전 DELETE 는 200 ack 였다.)
- **Origin 검증**(모든 `/mcp` 요청, 라우팅·인증보다 앞): Origin 헤더 부재 → 통과(비브라우저 클라이언트). loopback(`localhost`/`127.0.0.1`/`[::1]`, 임의 포트) → 통과. `MCP_ALLOWED_ORIGINS` 정확 일치 → 통과. 그 외 → **403**(no-store). DNS rebinding 방어의 스펙 MUST.

## legacy 경로 계약 (호환 계층)

`initialize`/`notifications/initialized` handshake, `ping`, modern 표식이 없는 `tools/list`, `tools/call`, JSON-RPC 배치 전부 종전 그대로 동작한다. 응답 봉투도 바이트 호환이다(modern 필드를 섞지 않는다). 예외는 `pack_ingest`가 최상위 `status: "partial"`을 반환할 때다. 이 결과에는 outbox 재시도를 위해 `isError: true`를 추가한다. 기존 content receipt 필드는 그대로 두고 새 필드만 더한다. 이 결과의 `retryable`과 `reject_codes` 규칙은 아래 "pack_ingest partial 결과의 retryable 표"가 정한다. #136 의 변경은 정확히 하나였다: **initialize 가 미지 버전을 무검증 echo 하지 않는다.** 지원 legacy 버전은 그대로 echo, 버전 부재는 종전 fallback `2024-11-05`, 미지 및 modern 전용 버전은 서버가 서빙하는 최신 legacy(`2025-11-25`)를 제시하고 클라이언트가 진행 여부를 판단한다(legacy 협상 규칙).

**`tools/call` 형상 검증(#251)**: legacy 도 modern 과 **같은** 공유 검증기(`validate_tools_call_params`)를 dispatch 이전에 통과한다 — 비문자열 `name`, truthy 비객체 `arguments`, 비객체 `params` 는 -32602 다. 전에는 이 형상들이 dispatch 안에서 터져 내부 TypeError 가 JSON-RPC **성공 봉투**에 실려 나가거나(-32601 로 갈리거나) -32603 이 됐다. 도구를 실행시키던 입력은 하나도 바뀌지 않았다. 남은 era 차이는 정확히 하나: legacy 는 present + **falsy** 비객체 `arguments`(`null`/`[]`/`0`/`0.0`/`false`/`""`)를 종전대로 `{}` 로 정규화해 **도구를 실행한다**(modern 은 -32602). 그 형상은 오늘 성공하는 호출이므로 D절 제거 시점까지 유지한다 — `docs/mcp-legacy-transition.md` §3.

## pack_ingest partial 결과의 retryable 표

`pack_ingest`의 partial 결과는 content에 `retryable`(불리언)을 싣는다. `isError`는 이 규칙과 무관하게 모든 partial에서 true다. 이 표가 `retryable`과 `reject_codes`를 정하는 유일한 문서이며, 다른 문서와 코드 주석은 이 표를 가리킨다.

규칙: 모든 오류가 아래 "최종" 행에 속하고 저장 실패가 없으면 `retryable`은 false다. 하나라도 "재시도" 행의 오류가 있으면 true다. `retryable`이 false일 때만 `reject_codes`가 있고, 최종 행의 코드를 정렬해 중복 없이 담는다. `ok` 결과에는 두 필드가 없다. `pack_create` 응답에도 두 필드가 없다.

| 경로 | 오류 | 분류 | 코드 |
|---|---|---|---|
| 노드 루프 | 소유권 probe가 타 팩 귀속을 확인 | 최종 | `foreign_pack_identity` |
| 노드 루프 | 그래프가 노드 식별 충돌로 거부 | 최종 | `node_identity_conflict` |
| 노드 루프 | 문법 거부(노드 유형 확인, 또는 속성 검증: 필수 속성, null 허용, enum, 타입) | 최종 | `grammar_rejected` |
| 노드 루프 | probe 확인 불능, 저장 실패, 문법 검증을 실행하지 못한 예외, 그 밖의 예외 | 재시도 | |
| 엣지 루프 | 소유권 probe가 타 팩 귀속을 확인 | 최종 | `foreign_pack_identity` |
| 엣지 루프 | 문법 거부(관계 확인) | 최종 | `grammar_rejected` |
| 엣지 루프 | probe 확인 불능, 끝점 미해결, 엣지 식별 충돌, 저장 실패, 그 밖의 예외 | 재시도 | |
| 텍스트(`text_as_node` true) | 소유권 probe가 타 팩 귀속을 확인 | 최종 | `foreign_pack_identity` |
| 텍스트 | 노드 식별 충돌이고 대상이 같은 팩의 evidence LogEntry나 TextUnit이 아님이 확정, 또는 첫 파생 id(`#text`)를 만들 수 없음 | 최종 | `node_identity_conflict` |
| 텍스트 | 노드 식별 충돌이고 대상 노드를 읽을 수 없음 | 재시도 | |
| 텍스트 | 소유권 probe 확인 불능, 저장 실패, 그 밖의 예외 | 재시도 | |
| 파생 TextUnit | 파생 id의 소유권 probe가 타 팩 귀속을 확인, 또는 마지막으로 시도한 파생 id가 식별 충돌. `#text`가 소유권 probe를 통과한 다른 노드나 다른 본문을 담고 있으면 마지막 파생 id는 본문 digest id다. 단, digest id가 id 길이 예산을 넘으면 digest id는 시도하지 않고 `#text` 충돌을 이 코드로 보고한다(아래 "이어진 세션의 본문" 절) | 최종 | `derived_id_conflict` |
| 파생 TextUnit | probe 확인 불능, 문법 거부, 저장 실패, 그 밖의 예외 | 재시도 | |
| legacy 텍스트(`text_as_node` false) | 소스 probe가 타 팩 귀속을 확인 | 최종 | `foreign_pack_identity` |
| legacy 텍스트 | source writer가 올린 노드 식별 충돌(compare-and-swap 경쟁일 수 있음), `ValueError`, 그래프 쓰기 불가, 스키마 이전 필요, probe 확인 불능, 저장 실패 | 재시도 | |

텍스트 경로(직접, 파생, legacy)의 문법 거부는 아직 최종 거부로 세지 않는다. 표의 해당 행은 재시도다.

표에 없는 예외(예: legacy 텍스트 경로에서 위 예외 밖의 예외)는 `pack_ingest` 도구 오류로 전파되어 partial 결과를 만들지 않는다.

최종 거부의 의미: 외부에서 데이터를 고치지 않는 한 같은 입력을 다시 보내도 성공할 수 없다. 클라이언트는 `retryable: false`를 읽어 재시도를 멈출 수 있다(#480).

## 이어진 세션의 본문과 격리 payload 재적재 (#483, #488)

같은 세션을 다른 LogEntry 속성(예: 더 늦은 `ended_at`)으로 다시 보내면 LogEntry 쓰기는 계속 `node_identity_conflict`로 거부된다. 본문은 파생 TextUnit 두 슬롯에 차례로 저장을 시도한다. Claude 세션은 LogEntry 없이 첫 본문을 `source_id` 자체에 TextUnit으로 저장한다. 그 TextUnit과 다른 본문이 오면 본문은 같은 두 슬롯에 저장을 시도한다(#488). 슬롯 규칙의 정본은 `_ingest_into_pack` docstring이다.

- 슬롯 1은 `derive_text_id(source_id)`, 곧 `<id>#text`다. 슬롯 1이 비어 있으면 본문은 여기에 저장을 시도한다. 슬롯 1 id가 id 길이 예산을 넘어 만들어지지 않으면 본문은 `node_identity_conflict`로 거부된다.
- 슬롯 1에 다른 노드나 다른 본문이 있고 소유권 probe가 통과하면 슬롯 2 `derive_text_id(source_id, digest)`에 저장을 시도한다. `digest`는 정제한 본문의 UTF-8 바이트 sha256 앞 16 hex다. 슬롯 1을 타 팩 노드가 점유하면 본문은 `derived_id_conflict`로 거부되고 슬롯 2는 시도하지 않는다. 슬롯 2 id가 id 길이 예산을 넘어 만들어지지 않을 때도 슬롯 2는 시도하지 않고 본문은 `derived_id_conflict`로 거부된다. 경계는 docstring이 정한다.
- 같은 본문을 같은 메타데이터로 다시 보내면 같은 노드를 찾고 새 행을 만들지 않는다. 파생 슬롯의 본문은 `source_id` 속성으로 출처를 가리킨다. `source_id` 자체에 저장된 첫 본문은 그 속성이 없고 노드 id가 곧 출처다.

훅의 재전송처럼 요청에 LogEntry 재기록이 들어 있다고 하자. 본문이 슬롯 1이나 슬롯 2에 저장되고 다른 오류가 없으면 결과는 partial이고 오류는 LogEntry 거부 하나다. 이때 `retryable`은 false이고 `reject_codes`는 `["node_identity_conflict"]`다. 본문이 거부되거나 다른 오류가 있으면 위 retryable 표가 각 오류의 코드를 정한다.

Claude 훅이 이어진 세션을 보내면 세션 Document와 Claim의 속성이 바뀐다. 일부 턴 Evidence도 바뀔 수 있다. 그 노드들의 식별 충돌도 최종 거부다. opencrab-dump 훅은 `retryable: false` 영수증을 영구 거부로 보고 `localcrab-outbox/quarantine/`에 격리한다. 본문이 저장됐어도 격리한다.

### 이 변경 전에 격리된 payload의 재적재

격리되었다고 해서 본문을 잃은 것은 아니다. 같은 본문에 `ended_at`만 늦은 재전송도 격리된다. 이 변경 전에는 새 본문을 담은 재전송이 그 본문을 잃었다. `.reason` 파일에는 일반 영구 거부 표지만 있으므로 재전송이 필요한지는 서버 상태로 판정한다. 라이브 재적재는 리드가 수행한다.

1. 전제: 이 변경을 담은 서버를 배포했다. 훅은 #274를 머지한 판이다.
2. 대상: 세션 payload만 다룬다. `pack_ingest` 인자에 비어 있지 않은 `text`와 `source_id`가 있고 엣지가 없다. Codex 세션 payload는 노드가 하나다. 그 노드는 `node_id`가 `source_id`와 같은 evidence/LogEntry다. Claude 세션 payload는 `source_id`가 `claude/`로 시작한다. 그 노드는 resource/Document, claim/Claim, evidence/Evidence뿐이다. 그 밖의 격리 파일은 모두 그대로 둔다. 이 절차는 본문 저장만 판정하고 다른 거부 항목(예: 낡은 훅 형식의 문법 거부)은 판정하지 않는다.
3. 저장 확인(재전송 전후에 쓴다): 두 슬롯 id를 서버 함수 `derive_text_id(source_id)`와 `derive_text_id(source_id, digest)`로 계산한다. `digest`는 서버가 적용하는 `_clean_str`을 거친 본문의 UTF-8 바이트 sha256 앞 16 hex다. fork salt가 id 끝에 남으므로 문자열 연결로 id를 만들지 않는다. 후보 id는 이 두 슬롯 id와 `source_id` 자체다. 후보 가운데 하나가 일치하는 노드를 담으면 본문은 저장된 것이다. 일치하는 노드는 evidence/TextUnit이다. 그 `pack_id`는 payload의 팩과 같다. 그 `text`는 정제한 payload 본문과 같다. 슬롯 id의 노드는 `source_id` 속성이 payload의 `source_id`와 같아야 한다. `source_id` 자체의 노드는 id 일치로 출처를 확인한다.
4. 절차(운영자, 대상 하나씩, opencrab-dump 훅 README 절차를 따른다):
   1. 저장 확인을 한다. 참이면 본문을 잃지 않았다. 6단계로 간다.
   2. 파일을 outbox에 `reingest483-<원래 이름>`으로 복사한다. 접두는 사본과 그 재격리본을 찾기 쉽게 한다. 원본은 격리에 둔다.
   3. 전량 flush가 사본을 보내게 한다. flush는 `LC_FLUSH_MIN_AGE`보다 새 파일을 건너뛴다.
   4. flush 뒤 사본의 위치는 영수증이 정한다. 어느 경우도 실패가 아니다.
      - `ok`: flush가 사본을 지운다.
      - `partial`이고 `retryable: false`: flush가 사본을 다시 격리한다. 이름은 `<줄기>.<epoch>.<pid>.json`이 될 수 있다. Codex payload는 LogEntry 거부가 그 원인이다. Claude payload는 Document, Claim, 턴 Evidence의 식별 충돌이나 낡은 훅 형식의 문법 거부가 그 원인이다.
      - `partial`이고 `retryable: true`(예: 저장 실패): 사본이 outbox에 남는다. 5번 재개 절차대로 다음 flush를 기다린다.
   5. 저장 확인을 다시 한다. 거짓이면 모든 파일을 보존하고 멈춘다. 서버가 본문을 거부한 것이다(예: 타 팩, 슬롯 id를 점유한 노드).
   6. 저장 확인이 참이고 outbox에 사본이 없다. 격리에 재격리된 사본이 있으면 그 사본과 `.reason`을 정확한 이름으로 지운다. 그다음 원본과 그 `.reason`을 정확한 이름으로 지운다.
5. 재개: 어느 단계에서 멈췄든 먼저 outbox에서 `reingest483-` 사본을 찾는다.
   - outbox에 사본이 있으면 flush가 아직 그 항목을 맡고 있다. 저장 확인 결과와 관계없이 다음 flush를 기다린다(3단계). 파일을 지우지 않는다. 재시도 대상 영수증(예: 그래프 쓰기 뒤 문서 저장 실패)을 받은 사본도 outbox에 남는다.
   - outbox에 사본이 없으면 저장 확인을 한다.
     - 참이면 6단계로 간다.
     - 거짓이고 격리에 사본이 있으면 5단계의 결과다. 모든 파일을 보존하고 멈춘다.
     - 거짓이고 사본이 어디에도 없으면 2단계로 간다.
   같은 파일을 다시 보내도 같은 노드를 찾고 새 행을 만들지 않는다.

서버 쪽 동작의 재현 명령: `pytest tests/test_issue74_ingest_graph_parity.py -k "483 or 488"`.

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
| `MCP_PROTOCOL_VERSIONS` | 쉼표 구분 부분집합으로 지원 버전 제한 (미설정 = 전체). legacy 전부 제외 = handshake 비활성(D절 이행 레버 — 이 modern-only 구성의 transport 오류 status 는 위 "상태 매핑" 참조), modern 전부 제외 = legacy 고정 | 빌드가 모르는 버전이 있으면 **기동 거부** |
| `MCP_ALLOWED_ORIGINS` | 쉼표 구분 Origin allowlist. `http(s)://host[:port]` 형태만, 브라우저가 보내는 형태 그대로(기본 포트 생략) | 경로·쿼리·userinfo 포함, `null`, 비-http 스킴이면 **기동 거부** |

두 설정 모두 standalone(`opencrab serve`)과 apps/api 양쪽에서 서빙 시작 전에 검증된다. 두 설정 모두 **완전히 빈 값(미설정·빈 문자열)은 기본값**이지만, **구분자·빈 항목만 있는 값(예: `,`, `a,,b`)은 malformed 로 기동 거부**된다 — 오타로 항목이 사라진 채 조용히 뜨는 것을 막는다.

**프로덕션 재기동 주의**: 알려진 소비자는 전부 Origin 헤더를 보내지 않아 기본값으로 안전하다. Origin 을 싣는 새 소비자가 403 을 받으면 코드 원복 없이 `MCP_ALLOWED_ORIGINS` 에 그 Origin 을 추가하고 재기동하면 된다.

## 범위 밖 (후속)

- **`subscriptions/listen` / `listChanged`**: 도구 레지스트리가 import 시점에 고정되어 프로세스 수명 동안 목록이 변하지 않으므로, capability 를 선언하지 않는 것이 진실한 선언이다. 목록이 런타임에 변하게 되면 그때 #150 의 principal 별 통지 문제와 함께 설계한다. #135 가 가시 뷰 기준 카탈로그 fingerprint(`tool_search` 의 `catalog_version`)를 도입했지만 목록은 여전히 불변이므로 이 결정은 유지된다(docs/mcp-tool-catalog.md).
- **MRTR / tasks extension**: 서버발 요청(roots/sampling/elicitation)이 코드베이스에 없어 적용 대상이 없다.
- **structuredContent**: 소비자가 생기면 추가한다.
- **D절 제거(ping·legacy handshake 삭제)**: deprecation 기간 이후 별도 결정. 이행 레버는 `MCP_PROTOCOL_VERSIONS`(legacy 제외 구성)로 이미 존재한다. 소비자 매트릭스, 제거 승인 조건, 관측 수단 부재와 롤백 경로는 **`docs/mcp-legacy-transition.md`** 가 정본이다(추적 이슈 #251). 기간은 아직 정해지지 않았다.
- **HTTP+SSE (2024-11-05 구 전송)**: 종전부터 미구현이며 새로 추가하지 않는다.

## 재현 명령 (살아 있는 상태 확인)

```bash
# 계약 테스트 전체 (modern-only·dual·legacy-only 구성 분리 계약과 legacy 형상 계약 포함)
.venv/bin/pytest tests/test_mcp_protocol_2026.py tests/test_http_app_modern.py \
    tests/test_http_app_modern_only.py tests/test_mcp_legacy_call_shape.py -q

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
