# #511 `ontology_query` 결과 문자열 길이 상한과 응답 크기 예산 설계

## 범위와 판정

MCP 도구 `ontology_query`는 결과 항목의 본문(`text`)과 `metadata`, `graph_context`에 든 문자열을 길이 제한 없이 응답에 싣는다. 노드 하나의 본문이 수십만 자이면 `limit=1` 질의의 응답도 같은 크기가 된다. 이 변경은 두 겹으로 응답 크기를 묶는다.

1. 항목 상한. 결과 항목 안의 저장 데이터 문자열 하나가 `QUERY_FIELD_CHAR_LIMIT`을 넘으면 앞부분만 싣고 잘린 자리와 원래 길이를 `truncation`에 싣는다. 전체 본문은 기존 도구 `ontology_get_node`로 얻는다.
2. 응답 예산. 항목 상한을 적용한 응답의 전송 길이(아래 "응답 예산의 단위")가 `QUERY_RESPONSE_CHAR_BUDGET`을 넘으면 결과를 싣지 않고 고정 내용의 오류 응답을 돌려준다. 이 겹은 항목 상한이 덮지 못하는 자리(식별자, 긴 키, 많은 문자열, 많은 결과)를 한 곳에서 막는다. 결과를 일부만 싣는 방식은 쓰지 않는다. `total`과 `results`의 관계가 달라지기 때문이다.

응답이 두 겹 안에 드는지는 시험이 판정한다.

이 변경이 하지 않는 일:

- CLI `query`와 REST `/api/query`. 두 표면은 `QueryResult.to_dict()`의 값을 그대로 쓰고 이 변경은 건드리지 않는다.
- `ontology_get_node`, `ontology_list_nodes`와 그 밖 도구의 응답 크기.
- 점수, 정렬, 접기 규칙, 검색 leg의 후보 선택.
- 전송 직렬화의 `ensure_ascii` 설정. 서버는 `ensure_ascii=True`로 직렬화하므로 한글 한 글자는 안쪽 문자열에서 6글자이고 봉투 안에서 7글자다. 이 설정을 바꾸면 모든 도구의 전송 형태가 달라지므로 후속 후보로 올린다.

## 현상 재확인과 크기를 키우는 자리

`HybridQuery.query()`의 각 leg가 결과 항목에 문자열을 싣는 자리는 다음과 같다. 소스에서 읽은 사실이다.

| 자리 | 출처 | 현재 길이 제한 |
| --- | --- | --- |
| `text` (벡터 leg) | 벡터 스토어의 문서. `write_source`가 쓴 원문 또는 `builder.add_node`가 노드 속성으로 합성한 문자열(`bm25._node_text`) | 없음 |
| `text` (FTS leg) | `doc_sources.text` | 없음 |
| `text` (BM25 leg) | `bm25._node_text(doc)`: `node_id`, `node_type`, 본문 계열 속성, 그 밖 속성의 키와 값을 이은 합성 문자열. 본문 계열 속성(`_TEXT_FIELDS`)은 전문이 들어가고 그 밖 속성의 값은 `_flatten_property_text`의 길이와 깊이 제한을 받는다. 속성 키는 제한이 없다 | 본문 계열 속성과 키는 없음 |
| `text` (그래프 leg) | `_property_text(props, relation)` | 4000자 |
| `metadata` (벡터 leg) | 적재 때 넣은 메타데이터 | 없음 |
| `metadata` (FTS leg) | `doc_sources.metadata` JSON | 없음 |
| `metadata` (그래프 leg) | 이웃 노드의 속성 전체(`metadata=props`). `TextUnit` 이웃이면 `text` 속성에 본문 전체가 든다 | 없음 |
| `metadata.folded_duplicates.members[i]` | 접힌 결과의 `to_dict()` 전체 | 없음 |
| `graph_context` | `anchor_id`, `labels`, `relation_type`, `relationship_types`, `depth`, `edge_endpoints`(식별자 둘), 보강이 더하는 `edge`(정규 노드 참조 둘과 `relation`) | 없음 |
| `selected_packs[i].matched` | 팩 선택이 만든 문자열 목록. 팩 메타데이터에서 나온 `source:<source_label>` 항목이 있다. `score_pack` 경로와 내용 검색 대체 경로가 만든다 | 없음 |
| `spaces_filter_warnings`, `pack_filter.warnings` | 고정 문구, 팩 id를 이어 붙인 문자열(`_coverage_warnings`), 예외 유형명 | 없음 |
| `subject_id`, `pack_filter.pack_ids` | 호출 주체와 팩 id 목록 | 없음 |

그래프 leg는 `text`를 4000자로 자르면서 같은 본문을 `metadata.text`에 통째로 싣는다. `text`만 자르면 이 경로가 상한을 우회한다. 접힌 항목의 `members`는 결과 항목 전체이므로 같은 이유로 대상이다. `selected_packs`의 `matched`는 팩 메타데이터가 직접 응답으로 나가는 경로이므로 항목 상한의 대상이다.

항목 상한의 대상이 아닌 자리와 근거:

- 식별자 자리: 최상위 `node_id`, `canonical`(노드 참조와 `document_id`), `graph_context`의 `anchor_id`, `edge_endpoints`, `edge`, `selected_packs[i].pack_id`, `subject_id`, `pack_filter.pack_ids`. 식별자를 자르면 다른 대상을 가리키는 값이 되고 `resolved`는 참으로 남는다(`opencrab/services/canonical_ids.py`의 규칙: 비슷한 id로 대체하지 않는다). 그래프 스키마는 `node_id`의 길이를 검사하지 않으므로 이 자리의 길이는 보장되지 않는다. 이 자리의 크기는 응답 예산이 막는다.
- 경고 문자열, 객체 키, 숫자와 불리언. 항목 상한이 덮지 않고 응답 예산이 막는다.
- 호출자 입력의 되돌림(`question`, `spaces_filter`)도 같다.

`metadata` 안의 `node_id`, `pack_id`, `id` 같은 값은 저장된 속성 값이므로 일반 문자열로 항목 상한을 받는다. 식별자 예외는 위 목록의 자리에만 걸린다.

## 상수

- `QUERY_FIELD_CHAR_LIMIT = 2000`. 단위는 Python 문자열의 글자 수(코드포인트)다. `ensure_ascii=True` 직렬화에서 코드포인트 하나가 안쪽 문자열에서 차지하는 길이는 일반 ASCII 1글자, 따옴표와 역슬래시와 줄바꿈 같은 이스케이프 대상 ASCII 2글자 또는 6글자(제어문자), BMP 비ASCII 문자 6글자, 보충 평면 문자 12글자(서로게이트 이스케이프 둘)다. 봉투 직렬화가 안쪽의 역슬래시를 다시 이스케이프하므로 예산 단위(봉투 안 길이)에서는 일반 ASCII 1글자, 따옴표와 역슬래시 4글자, 줄바꿈 3글자, NUL 같은 제어문자 7글자, BMP 비ASCII 문자 7글자, 보충 평면 문자 14글자다. 따라서 문자열 하나는 이 값의 1배에서 14배까지 예산을 차지한다. 한 결과가 `text`와 `metadata.text`에 같은 본문을 싣는 그래프 leg 모양을 감안해 정했다.
- `QUERY_RESPONSE_CHAR_BUDGET = 500_000`. 단위는 아래 "응답 예산의 단위"가 정한다. 이 값은 이슈 #511이 보고한 약 62만 자 응답이 클라이언트에 닿지 못한 사실보다 작게 잡았다. 대상 클라이언트가 이 값을 받을 수 있는지는 측정하지 않았다. 값은 조정할 수 있는 상수이고 근거는 그 보고 하나다.

두 값은 모듈 상수다. 설정으로 열지 않는다.

## 알고리즘

새 모듈 `opencrab/services/result_caps.py`에 순수 함수를 둔다.

핵심 순회 `_cap(value, path, limit, cuts, skip_keys)`는 `(새 값, 바뀜 여부)`를 돌려준다. 바뀌지 않았으면 입력 객체를 그대로 돌려준다. 입력 객체와 그 안의 중첩 컨테이너를 바꾸지 않는다.

1. `str`이고 글자 수가 `limit`을 넘으면 `value[:limit]`을 돌려주고 `cuts`에 `{"path": path, "original_chars": 글자 수}`를 더한다. `limit` 이하이면 그대로 둔다.
2. `dict`이면 항목을 순서대로 순회한다. 최상위 호출에서만 `skip_keys`에 든 키의 값은 순회하지 않고 그대로 둔다. 키는 바꾸지 않는다. 값은 `path + [키]` 경로로 재귀한다. 바뀐 것이 있으면 같은 키 순서의 새 딕셔너리를 돌려준다.
3. `list`와 `tuple`이면 같은 방식으로 원소를 순회한다. 경로에는 원소 위치(정수)를 넣는다. 바뀐 `tuple`은 `tuple`로 돌려준다.
4. 그 밖의 값은 그대로 둔다.

항목 처리 `cap_query_item(item, *, limit, folded)`는 `QueryResult.to_dict()` 모양의 딕셔너리 하나를 받는다.

1. `text`와 `metadata`를 순회한다. `folded`가 참이면 `metadata`의 최상위 키 `folded_duplicates`는 건너뛴다. `graph_context`는 최상위 키 `anchor_id`, `edge_endpoints`, `edge`를 건너뛰고 순회한다. 경로의 첫 원소는 그 키 이름이다. `node_id`, `canonical`, `score`, `raw_score`, `source`는 순회하지 않는다.
2. `folded`가 참이면 `metadata["folded_duplicates"]["members"]`의 각 항목에 `cap_query_item(member, limit=limit, folded=False)`를 적용한다. 바뀐 항목이 하나라도 있으면 `members`만 바꾼 새 접기 블록과 그것을 담은 새 `metadata`를 만든다. `count`는 그대로 둔다. 바뀐 항목이 없으면 접기 블록은 입력 객체 그대로다. 구성원의 `node_id`와 `canonical`은 구성원 항목의 최상위 자리이므로 1번 규칙이 건드리지 않는다.
3. 항목이 바뀌었는지는 1번의 `cuts` 유무와 2번의 구성원 변경 유무를 합쳐 판정한다. 둘 다 없으면 입력 항목을 그대로 돌려준다. 하나라도 있으면 키 순서를 유지한 얕은 복사본에 바뀐 값을 넣는다. `cuts`가 비어 있지 않으면 마지막에 키 `truncation`을 더한다. 구성원만 바뀐 대표 항목에는 `truncation`을 더하지 않는다.

`truncation` 객체:

```json
{
  "limit_chars": 2000,
  "fields": [
    {"path": ["text"], "original_chars": 380000},
    {"path": ["metadata", "text"], "original_chars": 380000}
  ],
  "full_text_tool": "ontology_get_node"
}
```

- `fields`는 순회 순서대로 잘린 문자열마다 한 항목이다. `path`는 항목 안에서 그 문자열의 위치다. 경로의 키는 입력 객체 키 그대로이므로 긴 객체 키는 `path`에 그대로 실린다. `truncation`은 항목 상한의 대상이 아니다. 잘린 문자열이 많거나 키가 길어 `truncation`이 커지는 경우는 응답 예산이 막는다.
- `full_text_tool`은 전체 본문을 얻는 도구 이름이다. 인자는 그 항목의 `node_id`다.
- 접힌 `members`의 항목은 자기 `truncation`을 가진다.
- 잘리지 않은 항목에는 `truncation` 키를 더하지 않는다. 그 항목의 직렬화는 변경 전과 같다.

선택 팩 처리 `cap_selected_pack(entry, *, limit)`: `entry`를 `skip_keys={"pack_id"}`로 순회하고, 바뀌었으면 같은 형식의 `truncation`(`full_text_tool` 없음)을 더한 새 딕셔너리를 돌려준다.

`folded`는 호출자가 `QueryResult.folded_count`로 정한다. 호출자가 metadata에 쓴 같은 이름의 키를 접기 블록으로 오인하지 않기 위해서다(#482의 규칙과 같다). `folded`가 거짓이면 `metadata.folded_duplicates`도 일반 값으로 순회한다.

### 응답 예산의 단위

서버는 도구 결과를 `content_text = json.dumps(result, ensure_ascii=True, default=str)`로 문자열로 만든 뒤 그 문자열을 응답 봉투의 `content[].text`에 넣고 봉투를 다시 직렬화한다(`opencrab/mcp/server.py`의 호출 둘과 stdio 출력 호출, HTTP `JSONResponse`). 안쪽 문자열의 따옴표와 역슬래시가 봉투 직렬화에서 다시 이스케이프되므로 안쪽 길이는 전송 길이보다 짧다. 예산의 단위는 봉투 안에서의 길이다: `len(json.dumps(json.dumps(response, ensure_ascii=True, default=str)))`. 요청 id, `jsonrpc`, `content` 틀, `_meta`의 길이는 세지 않는다. 이 틀의 길이는 요청 id 길이를 빼면 상수다. HTTP 배치 요청은 응답마다 이 예산을 적용한다.

응답 예산 `check_response_budget(response, *, budget)`: 위 길이를 센다. 예산 이하이면 `None`을 돌려준다. 초과하면 아래 고정 오류 응답을 돌려주고 실제 길이를 서버 로그(`WARNING`)에 남긴다.

```json
{
  "error": "response_too_large",
  "message": "<고정 문구>"
}
```

`message`는 모듈 상수 문자열이다. `limit`을 줄이거나 `pack_ids`로 범위를 좁혀 다시 호출하라고 안내하고 예산 상수 값을 문구에 싣는다. 호출자 입력, 저장 데이터, 가변 수치를 싣지 않으므로 오류 응답은 입력과 무관하게 항상 같은 값이다.

## 호출 지점

`opencrab/mcp/tools/query.py`의 `ontology_query`가 유일한 호출자다. 순서는 다음과 같다.

1. 정규 id 보강(`enrich_canonical`).
2. 각 결과에 `cap_query_item`. `include_pack_provenance`일 때 `selected_packs`의 각 항목에 `cap_selected_pack`.
3. 응답 딕셔너리를 모두 만든 뒤 `check_response_budget`. 오류 응답이 나오면 그것을 돌려준다. 이 검사는 `try` 안에서 돈다. 직렬화가 실패하면 기존 오류 처리(`safe_tool_error`)를 탄다.

보강이 먼저 돌아야 `canonical`이 접힌 항목에도 붙는다. 응답의 나머지 필드는 바꾸지 않는다. 도구 설명 문자열(`description`)과 docstring에 새 응답 필드와 오류 응답을 적는다. 입력 스키마는 바꾸지 않는다.

서버가 같은 응답을 다시 직렬화하므로 직렬화는 두 번 일어난다. 예산 검사는 `json.dumps` 두 번(안쪽 결과 문자열과 봉투 문자열)과 길이 비교를 더한다.

## 호출자와 형제 경로

- `QueryResult.to_dict()` 호출자: MCP `ontology_query`(이 변경 대상), CLI `query`, REST `/api/query`, 접기 단계(`to_result`). 접기와 점수 계산은 상한 적용 전의 원본 값을 쓴다. 상한은 응답 직전 한 곳에서만 걸리므로 접기 키(저장 본문의 집합)는 영향이 없다.
- CLI와 REST는 같은 `to_dict()` 값을 쓰지만 이 함수를 거치지 않는다. 두 표면의 계약은 그대로다.
- `selected_packs`를 만드는 호출자는 `resolve_packs`이고 CLI도 쓴다. 상한은 MCP 응답을 만드는 자리에서만 건다.
- 서버는 도구 결과를 두 자리(`opencrab/mcp/server.py`의 `ensure_ascii=True` 호출 둘: legacy 경로와 modern 경로)에서 문자열로 만들고, 전송 경로(stdio, HTTP)가 봉투를 다시 직렬화한다. 예산 검사는 안쪽 호출과 같은 인자에 봉투의 문자열 이스케이프를 더해 잰다.
- 새 오류 응답은 기존 오류 응답과 같은 최상위 키 `error`를 쓰고 `error`가 문자열이다. modern 경로는 이를 `isError=true`로 표시하고 legacy 경로는 본문에 싣는다(기존 동작).

## 전체 본문 경로의 범위

`truncation.full_text_tool`은 `ontology_get_node(node_id)`를 가리킨다. 이 도구가 원문을 돌려주는 조건은 항목의 `node_id`가 읽기 범위 안의 그래프 노드 id이고 그 노드의 속성이 원문을 담는 것이다. 이 조건이 성립하면 응답의 `properties`가 노드 속성 전체를 담는다. 조건이 성립하지 않는 경우는 아래와 같다.

- `write_source`가 소스마다 `evidence/TextUnit` 노드를 `source_id`로 만들고 그 `text` 속성에 원문을 담는다(#74). 벡터와 FTS leg는 메타데이터의 `node_id`를 저장 id보다 먼저 쓴다. `write_source`는 호출자가 준 `metadata.node_id`를 `source_id`에 맞추지 않는다. 호출자가 다른 값을 넣은 소스의 항목은 다른 노드를 가리킨다. 이때 `ontology_get_node`는 id가 없으면 `found=false`를 돌려주고, id가 읽기 범위 안의 다른 노드이면 그 노드를 돌려준다(`found=true`이고 원문이 아니다).
- 소스 id가 `SOURCE_NODE_ID_BUDGET`을 넘고 같은 id의 그래프 행이 하나도 없으면 `write_source`는 그래프 노드를 만들지 않고 문서와 벡터만 쓴다(기존 시험이 이 경로를 고정한다). 이 소스는 검색에 걸려도 `ontology_get_node`가 `found=false`로 답한다. 같은 id의 그래프 노드가 이미 있으면 갱신 경로를 타므로 이 예외에 들지 않는다.
- BM25 leg와 일반 노드 벡터의 `text`는 합성 문자열이다. 원래 값은 `properties`에서 읽는다.
- 그래프 leg 항목은 이웃 노드의 `node_id`로 위와 같은 조건이다.
- `ontology_get_node`가 돌려주는 본문 크기는 이 변경이 줄이지 않는다. 그 도구는 노드 전체를 요청하는 도구다.

설계는 `found=false`가 되는 소스를 위한 새 읽기 경로를 만들지 않는다. 문서에 한계로 적고 후속 후보로 올린다. 시험은 표준 `TextUnit` 경로의 성공, 그래프 행이 없는 긴 id 소스의 `found=false`, `metadata.node_id`가 없는 id를 가리키는 소스의 `found=false`, `metadata.node_id`가 다른 기존 노드를 가리키는 소스의 `found=true`와 원문이 아닌 본문을 판정한다.

## SOLID 판정

- 단일 책임: 새 모듈은 응답 크기 제한 하나만 한다. 호출자는 `ontology_query` 하나다.
- 개방 폐쇄: 해당 없음(항목별로 본문이 다른 분기를 만들지 않는다).
- 리스코프 치환: 해당 없음(대체 구현이 없다). 응답 항목의 기존 키와 값의 의미는 바꾸지 않는다. 상한을 넘은 문자열만 앞부분으로 줄고 `truncation` 키가 더해진다.
- 인터페이스 분리: `cap_query_item`은 항목, 상한, 접힘 여부만 받는다. `check_response_budget`은 응답과 예산만 받는다.
- 의존 역전: 상한과 예산은 키워드 인자로 받는다. 시험이 값을 바꿔 넣는다. 구현이 하나뿐이므로 별도 인터페이스 타입은 만들지 않는다.

## 시험 전략

단위 시험(`tests/test_query_result_caps.py`)과 `ontology_query` 경유 시험을 둔다. 성질은 아래 시험의 출력으로 판정한다.

- 상한 경계: 길이가 `limit`인 문자열은 그대로이고 `limit + 1`인 문자열은 `limit` 글자로 줄며 `original_chars`가 원래 길이다. 한글 문자열도 글자 수로 센다.
- 상한 이하의 항목: 여러 모양(`text`가 `None`, 빈 문자열, 중첩 메타데이터, 비문자열 값)에서 함수가 입력 객체를 그대로 돌려주고 `json.dumps` 결과가 변경 전과 바이트 단위로 같다.
- `text`, `metadata`, `graph_context` 각각: 하나만 긴 항목에서 `fields`의 경로와 원래 길이가 맞는다. 그래프 leg 모양(짧은 `text`, 긴 `metadata.text`)을 포함한다.
- 중첩: 딕셔너리, 목록, 튜플 안의 긴 문자열에서 경로가 맞는다. 입력 객체와 그 안의 컨테이너가 바뀌지 않는다(깊은 복사본과 비교). 긴 객체 키는 바뀌지 않는다.
- 접기: 대표와 구성원은 각각 자신에게 직접 잘린 문자열이 있을 때만 `truncation`을 가진다. 대표는 짧고 구성원만 긴 경우에 대표에는 `truncation`이 없고 새 접기 블록이 결과에 반영된다. 둘 다 긴 경우에는 둘 다 `truncation`을 가진다. `count`와 `members`의 항목 수와 순서가 그대로다. 호출자가 쓴 `folded_duplicates`(`folded=False`)는 일반 값으로 잘린다.
- 식별자: 상한을 넘는 값을 최상위 `node_id`, `canonical`, `graph_context.anchor_id`, `edge_endpoints`, `edge`, `selected_packs[i].pack_id`에 넣어도 그대로이고 `resolved`가 유지된다. 실제 `enrich()`를 거친 `graph_context.edge`로도 판정한다. 접힌 구성원의 `node_id`와 `canonical`도 그대로다.
- `selected_packs.matched`: 긴 `source:<label>` 항목이 잘리고 `truncation`이 붙는다. 짧은 항목은 변경 전과 같다.
- 응답 예산: 항목 상한 안의 문자열만 가진 응답이 예산 이하이면 변경 전과 같고, 초과한 응답은 `response_too_large` 오류다. 초과 사유가 서로 다른 응답(상한을 넘는 식별자 하나, 상한 이하의 문자열이 많은 결과, 긴 객체 키, 긴 경고, 많은 `truncation` 항목)마다 오류를 낸다. 오류 응답은 입력과 무관하게 항상 같은 값이다. 예산 경계(예산과 같은 길이는 통과, 하나 더 길면 오류)를 판정한다. 따옴표와 역슬래시가 많은 문자열로, 안쪽 직렬화 길이는 예산 이하이고 봉투 길이는 초과인 응답이 오류가 된다. 실제 서버의 도구 호출 경로(legacy와 modern)가 만든 응답 본문 길이를 재서 예산 단위가 실제 봉투 안 길이와 같은지 대조한다. 일반 ASCII 1, 따옴표와 역슬래시 4, 줄바꿈 3, NUL 7, 한글 7, 보충 평면 문자 14의 글자당 증가량을 대조한다. 상한 이하 문자열만 많아 예산을 넘는 응답은 오류가 되며, 이 변화는 의도한 계약 변경이다.
- `ontology_query` 경유: 큰 본문이 든 결과로 호출한 응답의 `results`와 `selected_packs`의 비식별자 문자열이 모두 상한 이하다. 짧은 결과의 응답은 변경 전 값과 같다. `include_canonical_ids`가 참일 때 접힌 항목의 `canonical`이 그대로다.
- leg별 실제 저장소 경로: 실제 `LocalGraphStore`, 벡터 스토어, 문서 저장소로 큰 `TextUnit` 본문을 넣고 `limit=1`로 질의한다. 벡터 leg(원문), BM25 leg(합성 문자열), 그래프 leg(`metadata.text`)가 각각 응답에 도달하는 경우를 하나씩 둔다. FTS leg는 지원하는 문서 저장소에서 둔다. 접힌 경로는 같은 본문을 가진 큰 노드 여러 개로 둔다. 각 시험은 해당 leg의 결과가 실제로 응답에 있음을 단언한다. 응답이 예산 안에 들고 `ontology_get_node`가 원래 본문 전체를 돌려준다. 긴 id 소스(그래프 행 없음)와 `metadata.node_id`가 없는 id를 가리키는 소스는 `found=false`이고, `metadata.node_id`가 다른 기존 노드를 가리키는 소스는 `found=true`이면서 원문이 아니다. 기존 그래프 행이 있는 긴 id는 원문을 돌려준다.
- 되돌림 시험(역변이): 상한 적용 호출 전체를 지우는 변이, 구성원 처리를 지우는 변이, `graph_context` 처리를 지우는 변이, `selected_packs` 처리를 지우는 변이, 예산 검사를 지우는 변이를 각각 넣어 각자 시험이 실패하는지 본다. 절차는 `docs/gate-recipes.md` 회귀 대사 절의 역변이 소절을 따른다.

## 받아들임 기준

- 항목 상한: 결과 항목의 `text`, `metadata`, `graph_context`(식별자 키 제외)에 있는 모든 문자열 값이 상한 이하다(접힌 구성원 포함). `selected_packs[i]`의 `pack_id` 외 문자열 값이 상한 이하다. 객체 키와 `truncation`의 경로는 이 기준에 들지 않는다. 그 크기는 응답 예산이 막는다.
- 잘린 문자열은 `truncation`에 위치와 원래 길이를 갖는다. 잘리지 않은 항목은 변경 전과 바이트 단위로 같다. 응답 단위의 동일성은 응답이 예산 안에 들 때만 성립한다. 상한 이하의 문자열만 있어도 예산을 넘는 응답은 오류가 된다. 이 변화는 의도한 계약 변경이며 문서에 적는다.
- 식별자 자리는 잘리지 않는다.
- 응답 예산: 성공 응답의 길이("응답 예산의 단위")는 예산 이하다. 초과하면 고정 내용의 `response_too_large` 오류를 돌려준다.
- `ontology_get_node`가 실제 저장소의 표준 `TextUnit` 노드에서 원래 본문 전체를 돌려준다. 돌려주지 못하는 조건은 문서에 적힌다.
- 기존 키의 이름과 의미가 그대로다. 성공 응답의 새 키는 항목과 선택 팩의 `truncation` 하나다. 예산 초과는 `error`가 `response_too_large`인 새 형태의 응답이다.
- 계약 변경이 `docs/ARCHITECTURE.md`와 도구 설명에 적힌다.
- 새 시험이 구현을 되돌리면 실패하고 기존 시험과 린트가 통과한다.

## 범위 밖

- 결과 일부만 싣는 응답, 총량을 맞추는 자동 `limit` 조정.
- CLI `query`와 REST `/api/query`의 같은 상한과 예산.
- 상한과 예산의 설정화. 호출자가 더 긴 본문을 바라면 `ontology_get_node`를 쓴다.
- `ontology_get_node`가 큰 본문을 쪼개 돌려주는 방법.
- 그래프 노드가 없는 소스와 `metadata.node_id`가 다른 소스의 전체 본문을 MCP로 읽는 방법.
- 전송 직렬화의 `ensure_ascii=False` 전환.
- `LocalDocStore.upsert_source`가 본문을 4096자로 줄여 저장하는 기존 동작(이 저장소는 FTS를 지원하지 않아 FTS leg 표에 걸리지 않는다).
