# 중복코드 정리 — 발견 사항 및 영향 분석 기록

전수검사(2026-06-18, 브랜치 `refactor/dedupe-sweep`)에서 확인된 중복·불일치를 기록한다. 이 문서는 **현상과 "변경하면 어떤 영향이 생기는지"를 기록**하는 것이 목적이며, 처리 방향을 확정하거나 작업을 지시하지 않는다. 각 항목의 현재 동작은 특성화 테스트(`tests/test_common_utils_characterization.py` 36, `tests/test_structural_characterization.py` 44, `tests/test_service_paths_characterization.py` 33 = 총 113개)에 박제되어 있다.

---

## 1. MCP ↔ HTTP REST 응답 형식 불일치 (가장 중요)

동일한 데이터 경로(query, node/edge 쓰기)가 MCP 도구(`opencrab/mcp/tools.py`)와 HTTP REST(`apps/api/main.py`)에 별도 구현되어 있고, 응답 형식이 6가지로 다르다. HTTP 응답 소비자는 `apps/web/lib/api.ts`다(아래 영향 분석의 기준).

### 1.1 에러 처리 정책
- **현재 차이**: grammar 검증 실패 시 — MCP `ontology_add_node`는 예외를 흡수해 `{"error": "...", "valid": False}` **dict를 정상 반환(HTTP 200 상당)**. HTTP `add_node`는 `validate_node(...).raise_if_invalid()`의 `ValueError`를 잡지 않아 **500 Internal Server Error**.
- **변경 시 영향**:
  - HTTP를 MCP식(검증실패를 200+에러바디)으로 맞추면 → REST 의미론 위반(실패가 200). 또한 `apps/web/lib/api.ts`의 query/ingest는 `r.ok`가 false일 때만 `err.detail`을 읽으므로, 200으로 내려오는 에러바디를 **에러로 인식하지 못하고 정상 데이터로 오해**.
  - MCP를 HTTP식(예외→4xx)으로 맞추면 → MCP 클라이언트(Claude)는 도구 응답이 dict이길 기대. 예외가 MCP 프로토콜 레벨 에러로 바뀌면 Claude의 에러 핸들링/재시도 흐름이 달라짐.
  - 어느 쪽으로 통일하든 한쪽 소비자의 에러 분기 코드가 깨지므로 **소비자 동시 수정이 필수**.

### 1.2 검증 깊이
- **현재 차이**: MCP는 `OntologyBuilder.add_node` 경유 → grammar 필수필드까지 검증(예: `User`의 `email`/`role` 누락 시 거부). HTTP `add_node`는 `validate_node`(space/node_type 존재만) 호출 → **필수필드 누락도 200 통과**.
- **변경 시 영향**:
  - HTTP를 builder 경유로 통일하면 → 지금까지 HTTP로 **필수필드 없이 통과되던 노드 쓰기가 거부**됨. 기존에 HTTP를 통해 불완전 노드를 넣어온 클라이언트/데이터 파이프라인이 있다면 회귀로 보일 수 있음(실제로는 검증 강화). 기존 데이터 정합성 점검 필요.
  - MCP를 HTTP식(얕은 검증)으로 낮추면 → 데이터 품질 저하, grammar 의미 약화. 권장되지 않음.

### 1.3 stores 상태 키 명명
- **현재 차이**: MCP node 응답 stores 키 = `neo4j/mongodb/postgres/chroma`(백엔드 제품명), MCP edge = `neo4j/postgres/mongodb`(+`"audited"`). HTTP node = `graph/documents/sql`(역할명), HTTP edge = `graph/sql`(documents 없음).
- **변경 시 영향**:
  - 백엔드 중립 명칭(`graph/docs/sql/vector`)으로 통일하면 → 로컬 모드(SQLite/Kuzu)에서 키가 `neo4j`인 현 MCP 응답의 **혼란 제거**(로컬인데 neo4j 키). 단 MCP 응답의 stores 키를 읽는 기존 소비자(있다면)가 깨짐.
  - `apps/web/lib/api.ts`는 stores 키를 읽지 않음(getNodes/getEdges/query/ingest만 사용) → **web 영향 없음**. 단 add_node/add_edge 응답을 읽는 다른 소비자가 있는지 확인 필요.

### 1.4 query envelope 키 집합
- **현재 차이**: MCP query = `question/spaces_filter/subject_id/tenant_id/pipeline/total/results/selected_packs/pack_filter`. HTTP query = `question/spaces_filter/total/results/keyword_fallback`. → `selected_packs/pack_filter/pipeline/tenant_id/subject_id`는 MCP 전용, `keyword_fallback`은 HTTP 전용.
- **변경 시 영향**:
  - 공통 envelope로 통일하면 → `apps/web/lib/api.ts:query()`는 응답 JSON을 그대로 반환하고 하위 컴포넌트가 `results`(항목 구조 `node_id/score/text/metadata`)를 소비. envelope 상위 키 추가/제거는 web의 `results` 의존을 깨지 않음. 단 **`results` 항목의 키 구조는 보존 필수**(node_id/score/text/metadata).
  - HTTP에 `selected_packs`/`pack_filter`를 추가하면 pack-aware 기능을 web에서도 노출 가능(기능 추가), 제거 방향이 아니면 호환 위험 낮음.

### 1.5 owner_id 주입 경로
- **현재 차이**: HTTP는 `X-User-Id` 헤더를 `properties.owner_id`로 자동 주입. MCP는 tenant stamp 경로(`tenant_id`/`subject_id`)로 소유권 표현.
- **변경 시 영향**: 공통 stamping으로 통일하면 → 두 인터페이스의 소유권/멀티테넌시 표현이 일치(rebac/billing 일관성↑). 단 HTTP의 `owner_id` 속성에 의존하는 기존 쿼리/필터가 있으면 키 위치 변경 시 깨짐. `backfill_owner_id.py` 스크립트와의 정합성 확인 필요.

### 1.6 receipt_id / receipt_ts
- **현재 차이**: builder 경유(MCP) 응답에만 `receipt_id`/`receipt_ts` 존재. HTTP 직접 쓰기 응답엔 없음.
- **변경 시 영향**: builder 경유로 통일하면 HTTP 응답에도 receipt가 생겨 감사 추적 일관성↑. 응답에 키가 추가될 뿐이라 소비자 깨짐 위험은 낮으나, HTTP 직접 멀티스토어 쓰기(`mode="direct"`)를 유지할 경우 receipt 생성 위치(builder 밖)를 별도 처리해야 함.

### 1.7 종합 영향 요약
- **공통 위험**: 어느 방향으로 통일하든 (a) `apps/web/lib/api.ts`의 에러 분기(`detail` 의존)와 `results` 항목 구조, (b) MCP 클라이언트(Claude)의 도구 응답 형식 기대 — 둘 중 하나 이상이 영향을 받음. 통일 작업은 **소비자 동시 수정과 특성화 테스트 갱신**을 전제로 함.
- 현재 특성화 테스트는 위 6가지 차이를 *있는 그대로* 박제 중 → 통일을 진행하는 순간 해당 테스트들은 의도적으로 갱신 대상이 됨(회귀가 아니라 명세 변경).

---

## 2. stable_id 생성 불일치 (데이터 호환 위험)

- **현재 차이**:
  - `pack/neo4j_export._sha_id`, `scripts/export_pack_graph_from_neo4j.sha_id` = **SHA256**[:16], `prefix:digest`(콜론), **sorted-JSON** 인코딩 해시
  - `scripts/import_obsidian_vault.sha_id` = **SHA1**[:16], `prefix-digest`(대시), **원문 문자열** 해시
  - `crabharness/dedupe._compute_id` = **SHA256**[:16], **프리픽스 없음**, `source|key` 해시
  - 동일 입력 `("p","hello")`에서 세 결과 모두 상이(`p:5aa762ae383fbb72` / `p-aaf4c61ddcc5e8a2` / `5aa...`형태와 다른 bare hex).
- **변경 시 영향**: ID 생성 규칙(알고리즘/길이/구분자/인코딩)을 바꾸면 **같은 엔티티가 다른 ID로 생성**되어 기존 그래프의 노드와 매칭 실패 → 중복 노드 발생, 엣지 연결 끊김. 즉 형식을 통일하려면 **데이터 마이그레이션(기존 ID 재매핑)이 동반**되어야 하며, 순수 코드 통합만으로는 호환을 깰 수 있음. SHA1 사용처(obsidian)는 ID 용도라 충돌 위험은 실무상 낮으나, 신규 데이터 한정 SHA256 전환도 기존 vault 재수입 시 ID 변동을 유발.

## 3. slugify 한글 처리 불일치

- **현재 차이**: `mcp.tools._slugify`·`landscape.adapter._slug`는 `[^a-z0-9]` 제거 → **한글 전부 삭제**(`"한글노드"` → fallback `"pack"`/`"item"`). `import_obsidian_vault`의 slugify는 `[a-z0-9가-힣]` 보존 → `"한글노드"` 유지. fallback 기본값도 `pack`/`item`/`node`로 제각각.
- **변경 시 영향**: MCP pack id 등 한글 제목이 들어오는 경로에서 한글 제거는 **순한글 제목을 전부 동일 fallback(`"pack"`)으로 붕괴**시켜 서로 다른 팩이 같은 slug로 충돌할 수 있음. `allow_hangul=True`로 바꾸면 한글 slug가 생성되어 충돌은 줄지만, 기존에 `"pack"`/`"pack-2"`로 생성된 slug와 **불연속**(같은 제목이 이제 다른 id) → 기존 참조 깨질 수 있음. 한글 제거가 의도인지 버그인지는 제품 결정 사항.

## 4. now_iso vs dedupe 타임스탬프 포맷 불일치

- **현재 차이**: 5개 `_now_iso` 정의(execution/workflow·approvals, ontology/identity·promotion, billing/hooks)는 aware `...+00:00`. `crabharness/dedupe.py` inline은 naive + 리터럴 `"Z"`(`datetime.utcnow().isoformat()+"Z"`).
- **변경 시 영향**: dedupe를 aware 포맷으로 통일하면 → 문자열 표현이 `"Z"` → `"+00:00"`로 바뀜. dedupe가 생성한 타임스탬프를 **문자열 비교/정렬하거나 `"Z"` suffix를 파싱하는 소비처**가 있으면 깨짐. 시각 값 자체는 동일(둘 다 UTC)하나 직렬화 표현이 달라짐.

## 5. Neo4j 행 정규화 두 구현의 의미 차이 — 통합 보류(결정)

> **결정(Phase 3)**: `_stable_json`≡`jdump`, `_sha_id`≡`sha_id`(비트단위 동일)와 `sha256_file`은 통합했으나, **`clean_props`/`normalise_node`/`normalise_edge` 정규화 함수는 통합 보류**. 아래의 의미 차이가 6가지 이상이라 단일 함수 파라미터화는 동작 버그 위험이 크다. opencrab 베이스 + `LABEL_TO_SPACE` 등 도메인 매핑 주입 방식의 통합은 별도 과제로 남긴다.


`pack/neo4j_export.py`(opencrab)와 `scripts/export_pack_graph_from_neo4j.py`(스크립트)의 정규화 함수가 거의 동일하나 다음이 다름:
- **clean_props**: opencrab은 입력 dict를 **그대로(동일 객체) 반환** → 이후 변형 시 원본 공유(부작용 위험). scripts는 `dict(value)` **얕은 복사** 반환.
- **normalise_node**:
  - space: opencrab은 props.space/ontology_space만 보고 없으면 `""`. scripts는 `LABEL_TO_SPACE` 매핑(Document→resource, Evidence→evidence, Persona→subject)으로 **라벨에서 추론**.
  - node_type: opencrab은 props.node_type/type→없으면 `labels[0]`. scripts는 `LABELS` 우선순위 매칭→없으면 `labels[0]`.
  - opencrab만 `props["type"]` fallback과 `evidence_ids` fallback 지원.
- **normalise_edge**: from_id/to_id, relation, from_space/to_space 모두 opencrab은 graceful + fallback 풍부, scripts는 `LABEL_TO_SPACE` 추론·`rel_props.from_id` 지원하나 누락 키에서 **`KeyError`**.
- **변경 시 영향**: 한쪽으로 통일하면 → opencrab으로 수렴 시 scripts의 NVIDIA persona pack 특화(라벨 추론) 동작이 사라져 export 결과의 space/node_type이 달라짐(빈 문자열화). scripts로 수렴 시 누락 키 입력에서 graceful → `KeyError`로 바뀌어 기존에 통과하던 범용 입력이 실패. `LABEL_TO_SPACE`를 주입 파라미터로 분리하면 양쪽 동작을 모두 보존 가능. `_stable_json`≡`jdump`, `_sha_id`≡`sha_id`는 비트단위 동일(통합 영향 없음).

## 6. 기타 구조/설계 불일치 (참고)

- **codex_workers 아키텍처 불일치**: `landscape`만 promotion package를 풍부하게 생성, `github_trending`은 stub(5개 repo 시뮬레이션), `soeak`는 미지원. `validate_bundle` 골격은 3워커 동일(필드 체크만 다름)하나 `collect_bundle`은 도메인별로 완전히 다름(통합 불가). 워커 인터페이스 표준화는 별도 과제.
- **데드코드(비노출 MCP 도구)**: `mcp/tools.py`에 주석 처리/비노출 상태 — `ontology_ingest`, `ontology_extract`, `query_bm25`, `promotion_*`, `billing_*`, `identity_*`. 활성 여부 미정.
- **grammar 재검증 중복**: `grammar/validator.py`의 `validate_node/edge`를 builder·extractor가 각자 재호출(중복 검증). 제거 시 검증 누락 위험 vs 성능/일관성 이득.
- **`schemas/pack_registry.py` 네이밍 혼동**: `ontology/pack_registry.py`(콘텐츠팩 런타임 선택)와 이름만 겹침. 실제로는 다른 시스템(스키마팩 설치). 리네임 시 import 갱신 필요(저위험).
- **스토어 pack 필터 분산**: local/kuzu/neo4j가 같은 pack 필터 규칙을 각자 구현. 의도된 백엔드별 구현이나 드리프트 위험 → 크로스 백엔드 동등성 테스트 부재.
