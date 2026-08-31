# MCP 도구 카탈로그와 tool_search (#135)

연결 이후 클라이언트가 최신 도구 카탈로그를 발견하는 경로를 기록한다. 표준 경로는
언제나 `tools/list` 다(modern 경로는 ttlMs/cacheScope 신선도 계약 포함 — see
docs/mcp-protocol-compat.md). `tool_search` 는 그 위의 보조 discovery 표면이다.

## 카탈로그 스냅샷

`opencrab.mcp.tools._registry.get_tool_catalog(principal)` 이 단일 출처다.

- **가시 범위**: `tools/list` 와 동일한 노출 필터(`_tool_allowed`)를 공유한다.
  principal 의 access tier 밖 도구는 스냅샷·fingerprint 어디에도 반영되지 않는다 —
  숨김 도구는 미등록 도구와 구별 불가라는 #150 불변식이 fingerprint 채널에서도 성립한다.
- **entry 필드**: `name`, `description`, `inputSchema`, `access`(read/write/admin tier),
  `requires_write_lock`(교차 프로세스 write.lock 필요 여부 — access 분류와 다른 축이다.
  `ontology_query` 는 lock 불필요이지만 billing 부수효과 때문에 write tier 다).
- **결정성**: 배열 순서가 레지스트리 order 정렬 그대로다. 원시 order 값은 노출하지
  않는다(원격 뷰의 정수 갭이 숨김 슬롯을 시사하는 것을 막는다).
- **fingerprint**: 가시 entry 배열의 canonical JSON sha256. 같은 프로세스·같은 가시
  뷰에서는 상수다 — 레지스트리는 import 시점에 고정되기 때문이다. 레지스트리가
  런타임 가변이 되는 날, 이 값이 stale 응답 감지의 기반이 된다.
- 스냅샷은 매 호출 재계산되고 deepcopy 로 격리된다(반환값 변조가 레지스트리에
  닿지 않는다).

## tool_search 계약

항상 노출되는 bootstrap 표면이다(READ tier — 로컬·원격 모두 가시). 스토어를 만지지
않는 순수 레지스트리 조회라 billing 부수효과가 없다.

- 입력(전부 optional, **명시적 JSON null 은 생략과 동치**, 공개 inputSchema 도
  `["<type>","null"]` 로 이를 선언한다):
  - `query`: 대소문자 무시 부분 문자열. 비면 가시 카탈로그 전체.
  - `access`: `read`/`write`/`admin` 필터.
  - `include_schema`: true 면 각 entry 에 inputSchema 포함.
  - `limit`: 정수 >= 1. 없으면 무제한.
- 출력: `catalog_version`(fingerprint), `generated_at`, `total_matched`, `returned`,
  `tools`(name-매치 그룹 우선, 그다음 description-만 매치 그룹, 각 그룹은 카탈로그
  순서), `note`.
- **검색 결과는 실행 권한을 부여하지 않는다.** 실행은 언제나 exact name 의
  `tools/call` 이며 `dispatch_tool` 의 독립 tier 게이트를 그대로 지난다.
- 검색 대상은 MCP 도구 카탈로그뿐이다. content/schema pack 의 탐색은 전용 도구
  (`content_pack_list`, `schema_pack_list`)가 담당한다.

## 변경 통지에 관한 상태

레지스트리가 import 시점 고정이므로 `listChanged` 미선언이 진실한 선언이라는 #243
결정은 유지된다(docs/mcp-protocol-compat.md 의 범위 밖 절). 목록이 런타임에 변하게
되면 그때 principal 별 통지 문제와 함께 subscriptions/listen 을 설계하고, 그 전까지
클라이언트의 신선도 수단은 `tools/list` 의 ttlMs 재조회와 `catalog_version` 비교다.

## 재현 명령

```bash
# 카탈로그·검색 계약 전체
.venv/bin/pytest tests/test_tool_catalog_search.py -q

# 골든 도구 목록(이름·순서·tier)
.venv/bin/pytest tests/test_tool_registry_contract.py -q
```
