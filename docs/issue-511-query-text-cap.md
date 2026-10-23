# #511 `ontology_query` 결과 문자열 길이 상한과 응답 크기 예산 설계

## 범위와 판정

MCP 도구 `ontology_query`는 결과 항목의 본문(`text`)과 `metadata`, `graph_context`에 든 문자열을 길이 제한 없이 응답에 싣는다. 노드 하나의 본문이 수십만 자이면 `limit=1` 질의의 응답도 그 길이를 싣는다. 이 변경은 두 단계를 둔다.

1. 항목 상한. 결과 항목 안의 저장 데이터 문자열이 `QUERY_FIELD_CHAR_LIMIT`을 넘으면 앞부분만 싣고 잘린 자리와 원래 길이를 `truncation`에 싣는다. 전체 본문은 기존 도구 `ontology_get_node`로 읽는다.
2. 응답 예산. 항목 상한을 적용한 응답의 길이(아래 "응답 예산의 단위")가 `QUERY_RESPONSE_CHAR_BUDGET`을 넘으면 `results`의 뒤쪽 결과를 덜어 내고 새 필드 `results_truncated`와 `omitted_results`를 싣는다. 덜어 내어도 결과를 하나 이상 담은 응답을 만들 수 없을 때는 고정 문구의 오류 응답을 돌려준다.

`total`의 현재 의미는 반환한 결과 수다(`opencrab/mcp/tools/query.py`가 `len(results)`를 싣는다). 덜어 낸 응답의 `total`은 싣고 있는 `results`의 길이다. 후보 총수는 싣지 않는다.

설계는 동작을 조건과 결과의 표로 적는다. 응답 크기와 변경 전 응답과의 대응은 시험이 판정한다.

이 변경이 다루지 않는 것:

- CLI `query`와 REST `/api/query`. 두 표면은 `QueryResult.to_dict()`의 값을 그대로 쓰고 이 변경의 대상이 아니다.
- `ontology_get_node`, `ontology_list_nodes`와 그 밖 도구의 응답 크기.
- 점수, 정렬, 접기 규칙, 검색 leg의 후보 선택.

## 소스에서 읽은 사실: 크기를 키우는 자리

`HybridQuery.query()`의 각 leg가 결과 항목에 문자열을 싣는 자리는 다음과 같다.

| 자리 | 출처 | 현재 길이 제한 |
| --- | --- | --- |
| `text` (벡터 leg) | 벡터 스토어의 문서. `write_source`가 쓴 원문 또는 `builder.add_node`가 노드 속성으로 합성한 문자열(`bm25._node_text`) | 없음 |
| `text` (FTS leg) | `doc_sources.text` | 없음 |
| `text` (BM25 leg) | `bm25._node_text(doc)`: `node_id`, `node_type`, 본문 계열 속성, 그 밖 속성의 키와 값을 이은 합성 문자열. 본문 계열 속성(`_TEXT_FIELDS`)은 전문이 들어가고 그 밖 속성의 값은 `_flatten_property_text`의 길이와 깊이 제한을 받는다. 속성 키에는 제한이 없다 | 본문 계열 속성과 키는 없음 |
| `text` (그래프 leg) | `_property_text(props, relation)` | 4000자 |
| `metadata` (벡터 leg) | 적재 때 넣은 메타데이터 | 없음 |
| `metadata` (FTS leg) | `doc_sources.metadata` JSON | 없음 |
| `metadata` (그래프 leg) | 이웃 노드의 속성 전체(`metadata=props`). `TextUnit` 이웃이면 `text` 속성에 본문 전체가 든다 | 없음 |
| `metadata.folded_duplicates.members[i]` | 접힌 결과의 `to_dict()` 전체 | 없음 |
| `graph_context` | `anchor_id`, `labels`, `relation_type`, `relationship_types`, `depth`, `edge_endpoints`(식별자 둘), 보강이 더하는 `edge`(정규 노드 참조 둘과 `relation`) | 없음 |
| `selected_packs[i].matched` | 팩 선택이 만든 문자열 목록. 팩 메타데이터에서 나온 `source:<source_label>` 항목이 있다. `score_pack` 경로와 내용 검색 대체 경로가 만든다 | 없음 |
| `spaces_filter_warnings`, `pack_filter.warnings` | 고정 문구, 팩 id를 이어 붙인 문자열(`_coverage_warnings`), 예외 유형명 | 없음 |
| `subject_id`, `pack_filter.pack_ids` | 호출 주체와 팩 id 목록 | 없음 |

그래프 leg는 `text`를 4000자로 자르고 같은 본문을 `metadata.text`에 전체로 싣는다. 항목 상한은 `metadata`에도 걸린다. 접힌 항목의 `members`는 결과 항목 전체이므로 항목 상한의 대상이다. `selected_packs`의 `matched`는 팩 메타데이터를 응답으로 내보내므로 항목 상한의 대상이다.

항목 상한의 대상이 아닌 자리와 근거:

- 식별자 자리: 최상위 `node_id`, `canonical`(노드 참조와 `document_id`), `graph_context`의 `anchor_id`, `edge_endpoints`, `edge`, `selected_packs[i].pack_id`, `subject_id`, `pack_filter.pack_ids`. 식별자를 자르면 다른 대상을 가리키는 값이 되고 `resolved`는 참으로 남는다(`opencrab/services/canonical_ids.py`의 규칙: 비슷한 id로 대체하지 않는다). 그래프 스키마는 `node_id`의 길이를 검사하지 않는다.
- 경고 문자열, 객체 키, `truncation`의 `fields[].path`, 숫자와 불리언.
- 호출자 입력의 되돌림(`question`, `spaces_filter`).

대상이 아닌 자리의 크기는 응답 예산 단계에서 응답 길이에 들어간다.

`metadata` 안의 `node_id`, `pack_id`, `id` 같은 값은 저장된 속성 값이므로 일반 문자열로 항목 상한을 받는다. 식별자 예외는 위 목록의 자리에만 적용한다.

## 상수

- `QUERY_FIELD_CHAR_LIMIT = 4000`. 단위는 Python 문자열의 글자 수(코드포인트)다. 값의 근거는 선례 하나다. 그래프 leg의 `_property_text`가 `text`를 4000자로 자른다.
- `QUERY_RESPONSE_CHAR_BUDGET = 500_000`. 단위는 "응답 예산의 단위"가 정한다. 값의 근거는 관측 하나다. 이슈 #511이 보고한 약 62만 자 응답이 클라이언트에 닿지 못했고, 예산은 그 값보다 작게 잡았다. 대상 클라이언트의 수용 한도는 측정하지 못했다.

두 값은 모듈 상수다. 설정으로 열지 않는다.

문자당 바이트 증가량(예산 단위, 빈 문자열 대비 증가량): 일반 ASCII 1, 따옴표 4, 역슬래시 4, 줄바꿈 3, NUL 7, 한글 3, 보충 평면 문자 4다. 한글 4000자 문자열 하나는 예산 단위에서 12,000바이트다. 그래프 leg 항목에서 `text`와 `metadata.text`가 같은 한글 본문 4000자를 싣는 경우 이 두 문자열의 합은 24,000바이트다. 결과 항목의 다른 필드는 이 합에 들지 않는다.

## 알고리즘

새 모듈 `opencrab/services/result_caps.py`에 순수 함수를 둔다. 함수는 입력 객체에 대입하지 않고 바뀐 자리의 컨테이너를 새로 만든다.

### 문자열 순회 `_cap(value, path, limit, cuts, skip_keys)`

`(새 값, 바뀜 여부)`를 돌려준다. 결정표:

| 값의 종류 | 조건 | 동작 |
| --- | --- | --- |
| `str` | 글자 수가 `limit` 이하 | 입력 객체를 돌려준다. 바뀜 없음 |
| `str` | 글자 수가 `limit` 초과 | `value[:limit]`을 돌려준다. `cuts`에 `{"path": path, "original_chars": 글자 수}`를 더한다 |
| `dict` | 모든 값이 바뀜 없음 | 입력 객체를 돌려준다 |
| `dict` | 값 하나 이상이 바뀜 | 같은 키 순서의 새 딕셔너리를 돌려준다. 키는 입력 키 그대로 쓴다. 값은 `path + [키]` 경로로 재귀한다. 최상위 호출에서 `skip_keys`에 든 키의 값은 재귀하지 않고 입력 값을 쓴다 |
| `list`, `tuple` | 모든 원소가 바뀜 없음 | 입력 객체를 돌려준다 |
| `list`, `tuple` | 원소 하나 이상이 바뀜 | 같은 순서의 새 컨테이너를 같은 종류로 돌려준다. 경로에는 원소 위치(정수)를 넣는다 |
| 그 밖 | 해당 없음 | 입력 객체를 돌려준다 |

### 항목 처리 `cap_query_item(item, *, limit, folded)`

`QueryResult.to_dict()` 모양의 딕셔너리 하나를 받는다. 순회 대상은 다음이다.

- `text`, `metadata`. `folded`가 참이면 `metadata`의 최상위 키 `folded_duplicates`는 `skip_keys`에 둔다.
- `graph_context`. 최상위 키 `anchor_id`, `edge_endpoints`, `edge`는 `skip_keys`에 둔다.
- 경로의 첫 원소는 그 키 이름이다. `node_id`, `canonical`, `score`, `raw_score`, `source`는 순회하지 않는다.

`folded`가 참이면 `metadata["folded_duplicates"]["members"]`의 각 항목에 `cap_query_item(member, limit=limit, folded=False)`를 적용한다. 구성원의 `node_id`와 `canonical`은 구성원 항목의 최상위 자리이므로 순회 대상이 아니다.

결정표:

| 조건 | 동작 |
| --- | --- |
| 순회에서 잘린 문자열 없음, 바뀐 구성원 없음 | 입력 항목 객체를 돌려준다. `truncation`을 더하지 않는다 |
| 순회에서 잘린 문자열 있음 | 키 순서를 유지한 얕은 복사본에 바뀐 값을 넣고 마지막에 `truncation`을 더한다 |
| 순회에서 잘린 문자열 없음, 바뀐 구성원 있음 | 얕은 복사본의 `metadata`를 `members`만 바뀐 새 접기 블록(`count`는 입력 값)을 담은 새 딕셔너리로 바꾼다. `truncation`을 더하지 않는다 |
| 순회에서 잘린 문자열 있음, 바뀐 구성원 있음 | 위 두 행을 모두 적용한다 |

`folded`는 호출자가 `QueryResult.folded_count`로 정한다. 호출자가 metadata에 쓴 같은 이름의 키를 접기 블록으로 오인하지 않기 위해서다(#482의 규칙). `folded`가 거짓이면 `metadata.folded_duplicates`도 일반 값으로 순회한다.

### `truncation` 객체

```json
{
  "limit_chars": 4000,
  "fields": [
    {"path": ["text"], "original_chars": 380000},
    {"path": ["metadata", "text"], "original_chars": 380000}
  ],
  "full_text_tool": "ontology_get_node"
}
```

- `limit_chars`는 호출에 쓴 `limit`이다. 운영 호출에서 이 값은 `QUERY_FIELD_CHAR_LIMIT`이다.
- `fields`는 순회 순서대로 잘린 문자열마다 한 항목이다. `path`는 항목 안에서 그 문자열의 위치다. 경로의 키는 입력 객체 키를 그대로 쓰므로 긴 객체 키가 `path`에 실릴 수 있다.
- `full_text_tool`은 전체 본문을 읽는 도구 이름이다. 인자는 그 항목의 `node_id`다.
- 접힌 `members`의 항목은 자기 `truncation`을 가진다.

### 선택 팩 처리 `cap_selected_pack(entry, *, limit)`

`entry`를 `skip_keys={"pack_id"}`로 순회한다. 잘린 문자열이 없으면 입력 객체를 돌려준다. 있으면 새 딕셔너리에 같은 형식의 `truncation`(`full_text_tool` 없음)을 더해 돌려준다.

### 응답 예산의 단위

서버는 도구 결과를 UTF-8 JSON 문자열로 만든 뒤 그 문자열을 응답 봉투의 `content[].text`에 넣고 다시 직렬화한다. 안쪽 문자열의 따옴표와 역슬래시는 봉투 직렬화에서 다시 이스케이프된다. 예산의 단위는 이 문자열이 outer JSON string에서 차지하는 UTF-8 바이트 길이 `L(x) = len(json.dumps(serialize_tool_result(x), ensure_ascii=False).encode("utf-8"))`다. 요청 id, `jsonrpc`, `content` 틀, `_meta`, batch frame의 길이는 세지 않는다. 전체 전송 봉투의 길이는 이 단위가 아니다. HTTP batch 요청에는 응답마다 이 예산을 적용한다.

### 응답 예산 `apply_response_budget(response, *, budget)`

`n`은 `len(response["results"])`다. `B`는 `budget`이다. `R(k)`는 아래 구성으로 만든 응답이다.

`R(k)` 구성(`1 <= k <= n - 1`): `total`을 `k`로, `results`를 앞 `k`건으로 바꾸고 `results` 바로 뒤에 `results_truncated: true`와 `omitted_results: n - k`를 더한다. 나머지 키의 순서와 값은 입력 응답의 것이다.

결정표(위에서 아래로 처음 맞는 행을 적용한다):

| 행 | 조건 | 동작 |
| --- | --- | --- |
| A | `L(response) <= B` | `response`를 그대로 돌려준다. 새 필드를 더하지 않는다 |
| B | `L(response) > B`, `n >= 2`, `L(R(1)) <= B` | `L(R(k)) <= B`를 직접 잰 `k` 가운데 이분 탐색이 찾은 값(탐색 범위 `1..n-1`)으로 `R(k)`를 돌려준다. 돌려주기 전에 `L(R(k))`를 한 번 더 잰다. 서버 로그(`WARNING`)에 `L(response)`, `n`, `k`를 남긴다 |
| C | `L(response) > B`이고 (`n <= 1` 또는 `L(R(1)) > B`) | 고정 오류 응답을 돌려준다. `n == 0`인 경우와 결과 밖 필드(`selected_packs`, 경고, `pack_filter`)만으로 초과하는 경우가 이 행에 든다. 서버 로그(`WARNING`)에 `L(response)`와 `n`을 남긴다 |

행 B가 고른 `k`가 `L(R(k)) <= B`이고 (`k == n - 1` 또는 `L(R(k + 1)) > B`)인지는 시험이 판정한다.

고정 오류 응답:

```json
{
  "error": "response_too_large",
  "message": "<고정 문구>"
}
```

`message`는 모듈 상수 문자열이다. 문구: "응답 길이가 응답 예산(500000바이트)을 넘고, 결과를 덜어 내어도 예산 안에 드는 응답을 만들 수 없다. pack_ids나 spaces로 범위를 좁히거나 limit을 줄여 다시 호출하라. 개별 노드는 ontology_get_node로 읽는다." 이 문구는 결과의 개수와 초과 원인을 서술하지 않는다. 호출자 입력, 저장 데이터, 가변 수치를 문구에 넣지 않는다.

행 B에서 덜어 낸 결과는 응답에 싣지 않는다. 접힌 구성원은 대표 결과와 함께 덜어 낸다. 응답은 덜어 낸 결과의 수를 `omitted_results`로 싣고 어떤 결과인지는 싣지 않는다.

## 호출 지점

`opencrab/mcp/tools/query.py`의 `ontology_query`가 유일한 호출자다. 순서는 다음과 같다.

1. 정규 id 보강(`enrich_canonical`).
2. 각 결과에 `cap_query_item`. `include_pack_provenance`일 때 `selected_packs`의 각 항목에 `cap_selected_pack`.
3. 응답 딕셔너리를 모두 만든 뒤 `apply_response_budget`. 반환값을 돌려준다. 이 호출은 `try` 안에서 돈다. 직렬화가 실패하면 기존 오류 처리(`safe_tool_error`)를 탄다.

보강이 먼저 돌아야 `canonical`이 접힌 항목에도 붙는다. 도구 설명 문자열(`description`)과 docstring에 새 응답 필드(`truncation`, `results_truncated`, `omitted_results`)와 오류 응답을 적는다. 입력 스키마는 이 변경의 대상이 아니다.

서버가 같은 응답을 다시 직렬화하므로 직렬화가 두 번 일어난다. 예산 검사는 `L`의 계산마다 `json.dumps` 두 번을 더한다. 행 B는 이분 탐색이 `n`에 대해 로그 횟수만큼 `L`을 더 계산한다.

## 호출자와 형제 경로

- `QueryResult.to_dict()` 호출자: MCP `ontology_query`(이 변경 대상), CLI `query`, REST `/api/query`, 접기 단계(`to_result`). 접기와 점수 계산은 항목 상한 적용 전의 원본 값을 쓴다. 항목 상한은 응답 직전 한 곳에서 걸린다. 접기 키(저장 본문의 집합)는 항목 상한 적용 전에 정해진다.
- CLI와 REST는 같은 `to_dict()` 값을 쓰고 이 함수들을 거치지 않는다.
- `selected_packs`를 만드는 호출자는 `resolve_packs`이고 CLI도 쓴다. 항목 상한은 MCP 응답을 만드는 자리에서만 건다.
- 서버는 legacy와 modern 도구 호출에서 같은 UTF-8 결과 문자열을 만들고, stdio와 HTTP 전송 경로가 JSON-RPC 봉투를 직렬화한다.
- 새 오류 응답은 기존 오류 응답과 같은 최상위 키 `error`를 쓰고 값이 문자열이다. modern 경로는 이를 `isError=true`로 표시하고 legacy 경로는 본문에 싣는다(기존 동작).

## 전체 본문 경로의 범위

`truncation.full_text_tool`은 `ontology_get_node(node_id)`를 가리킨다. 이 도구가 원문을 돌려주는 조건은 항목의 `node_id`가 읽기 범위 안의 그래프 노드 id이고 그 노드의 속성이 원문을 담는 것이다. 이 조건이 성립하면 응답의 `properties`가 노드 속성 전체를 담는다. 조건이 성립하지 않는 경우는 다음과 같다.

- `write_source`가 소스마다 `evidence/TextUnit` 노드를 `source_id`로 만들고 그 `text` 속성에 원문을 담는다(#74). 벡터와 FTS leg는 메타데이터의 `node_id`를 저장 id보다 먼저 쓴다. `write_source`는 호출자가 준 `metadata.node_id`를 `source_id`에 맞추지 않는다. 호출자가 다른 값을 넣은 소스의 항목은 다른 노드를 가리킨다. 이때 `ontology_get_node`는 id가 없으면 `found=false`를 돌려주고, id가 읽기 범위 안의 다른 노드이면 그 노드를 돌려준다(`found=true`이고 원문이 아니다).
- 소스 id가 `SOURCE_NODE_ID_BUDGET`을 넘고 같은 id의 그래프 행이 하나도 없으면 `write_source`는 그래프 노드를 만들지 않고 문서와 벡터만 쓴다(기존 시험이 이 경로를 고정한다). 이 소스는 검색에 걸려도 `ontology_get_node`가 `found=false`로 답한다. 같은 id의 그래프 노드가 이미 있으면 갱신 경로를 타므로 이 예외에 들지 않는다.
- BM25 leg와 일반 노드 벡터의 `text`는 합성 문자열이다. 원래 값은 `properties`에서 읽는다.
- 그래프 leg 항목은 이웃 노드의 `node_id`로 위와 같은 조건이다.
- `ontology_get_node`가 돌려주는 본문 크기는 이 변경의 대상이 아니다. 그 도구는 노드 전체를 요청하는 도구다.

`found=false`가 되는 소스를 위한 새 읽기 경로는 이 변경의 범위 밖이다. 문서에 한계로 적고 후속 후보로 올린다.

## SOLID 판정

- 단일 책임: 새 모듈은 응답 크기 처리 하나를 맡는다. 호출자는 `ontology_query` 하나다.
- 개방 폐쇄: 해당 없음(항목별로 본문이 다른 분기가 없다).
- 리스코프 치환: 해당 없음(대체 구현이 없다). 응답 항목의 기존 키는 이름을 유지한다. 상한을 넘은 문자열만 앞부분으로 줄고 `truncation` 키가 더해진다.
- 인터페이스 분리: `cap_query_item`은 항목, 상한, 접힘 여부만 받는다. `apply_response_budget`은 응답과 예산만 받는다.
- 의존 역전: 상한과 예산은 키워드 인자로 받는다. 시험이 값을 바꿔 넣는다. 구현이 하나뿐이므로 별도 인터페이스 타입은 만들지 않는다.

## 시험 전략

단위 시험(`tests/test_query_result_caps.py`)과 `ontology_query` 경유 시험을 둔다. 시험은 위 결정표의 행과 1:1로 대응한다. 상한 시험은 `truncation.fields[].path`의 문자열 원소와 객체 키를 문자열 길이 검사에서 뺀다.

### 문자열 순회 표

| 행 | 시험 |
| --- | --- |
| `str` 이하 | 길이 `limit`인 문자열은 입력 객체가 그대로 돌아온다. 한글, 보충 평면 문자, 빈 문자열을 포함한다 |
| `str` 초과 | 길이 `limit + 1`인 문자열은 앞 `limit`글자가 돌아오고 `original_chars`가 원래 길이다. 한글은 글자 수로 센다 |
| `dict` 바뀜 없음, 바뀜 있음 | 두 경우의 반환 객체 종류, 키 순서, 입력 객체 비변경(깊은 복사본과 비교). 긴 객체 키는 입력 키 그대로다. `skip_keys`에 든 키의 긴 값은 그대로다 |
| `list`, `tuple` | 두 경우의 반환 종류와 경로의 정수 위치 |
| 그 밖 | 수, 불리언, `None`은 입력 객체가 그대로 돌아온다 |

### 항목 처리 표

| 행 | 시험 |
| --- | --- |
| 잘린 문자열 없음, 바뀐 구성원 없음 | 여러 모양(`text`가 `None`, 빈 문자열, 중첩 메타데이터, 비문자열 값)에서 입력 항목 객체가 돌아오고 `json.dumps` 결과가 입력의 것과 바이트 단위로 일치한다 |
| 잘린 문자열 있음 | `text`만 긴 항목, `metadata`만 긴 항목(그래프 leg 모양: 짧은 `text`, 긴 `metadata.text`), `graph_context`의 식별자 아닌 키만 긴 항목에서 `fields`의 경로와 원래 길이가 맞고 `truncation`이 마지막 키다 |
| 구성원만 바뀜 | 대표는 짧고 구성원만 긴 항목에서 대표에 `truncation`이 없고 새 접기 블록이 결과에 반영된다. `count`와 `members`의 항목 수와 순서가 입력과 일치한다 |
| 둘 다 바뀜 | 대표와 구성원이 모두 `truncation`을 가진다 |
| `folded=False` | 호출자가 쓴 `folded_duplicates` 값은 일반 값으로 순회한다 |
| 식별자 자리 | 상한을 넘는 값을 최상위 `node_id`, `canonical`, `graph_context.anchor_id`, `edge_endpoints`, `edge`, `selected_packs[i].pack_id`, 접힌 구성원의 `node_id`와 `canonical`에 넣으면 입력 값이 그대로 돌아온다. 실제 `enrich()`를 거친 `graph_context.edge`로도 확인한다 |
| `cap_selected_pack` | 긴 `source:<label>` 항목은 잘리고 `truncation`이 붙는다. 짧은 항목은 입력 객체가 그대로 돌아온다 |

### 응답 예산 표

| 행 | 시험 |
| --- | --- |
| A | `L(response)`가 `B`와 일치하는 응답은 입력 응답 객체가 그대로 돌아오고 `results_truncated`와 `omitted_results`가 없다. 전제: 항목 상한이 바꾼 자리가 없는 응답이면 `ontology_query`의 결과가 변경 전 응답과 바이트 단위로 일치한다(`ontology_query` 경유 시험이 변경 전 기대값을 직접 만들어 비교한다) |
| B | `L(response) = B + 1`인 응답, 결과 여러 건이 각각 항목 상한 이하이지만 합이 큰 응답, 상한을 넘는 식별자가 뒤쪽 결과에 있는 응답에서 다음을 확인한다. 앞쪽 결과가 순서대로 남는다. `total`이 남은 결과 수다. `omitted_results + total`이 원래 결과 수다. `L(R(k)) <= B`이고 (`k == n - 1` 또는 `L(R(k + 1)) > B`)다. `n = 2`에서 둘째 결과의 식별자만 커서 하나만 남는 응답(`k = 1 = n - 1`)을 포함한다. 접힌 구성원은 대표와 함께 덜어진다. `ontology_query` 경유로 `total`의 값이 반환한 결과 수임을 확인한다 |
| B, 자릿수 경계 | 결과 수가 9, 10, 99, 100, 999, 1000 주변인 응답에서 `k`와 `n - k`의 자릿수가 바뀌는 경계에 최대 `k`가 놓이게 구성하고 `L(R(k)) <= B`이고 (`k == n - 1` 또는 `L(R(k + 1)) > B`)임을 확인한다 |
| C, `n == 1` | 한 결과가 식별자 하나로 예산을 넘는 응답은 고정 오류 응답이다 |
| C, `n >= 2`, `L(R(1)) > B` | 첫 결과 한 건이 4000자 문자열 156개를 담은 응답은 고정 오류 응답이다 |
| C, `n == 0` | 결과가 0건이고 긴 경고로 예산을 넘는 응답은 고정 오류 응답이다 |
| C, 결과 밖 필드 | 결과 항목이 작고 `selected_packs`나 경고로 예산을 넘는 응답은 고정 오류 응답이다 |
| 오류 응답 | 위 네 경우의 오류 응답이 서로 일치하고 입력 크기에 따라 달라지지 않는다. 문구가 결과 개수와 초과 원인을 서술하지 않는다 |
| 단위 | 실제 서버의 도구 호출 경로(legacy와 modern)가 만든 `content[].text`의 직렬화 길이를 재서 `L`과 대조한다. 글자당 증가량 1, 4, 4, 3, 7, 7, 14를 일반 ASCII, 따옴표, 역슬래시, 줄바꿈, NUL, 한글, 보충 평면 문자로 대조한다. 안쪽 직렬화 길이는 `B` 이하이고 `L`은 `B` 초과인 응답(따옴표와 역슬래시가 많은 문자열)은 행 A에 들지 않는다 |

### 실제 저장소 경로

실제 `LocalGraphStore`, 벡터 스토어, 문서 저장소로 큰 `TextUnit` 본문을 넣고 `limit=1`로 질의한다. 벡터 leg(원문), BM25 leg(합성 문자열), 그래프 leg(`metadata.text`)가 각각 응답에 도달하는 경우를 하나씩 둔다. FTS leg는 지원하는 문서 저장소에서 둔다. 접힌 경로는 같은 본문을 가진 큰 노드 여러 개로 둔다. 각 시험은 해당 leg의 결과가 응답에 있음을 단언하고, 응답의 `results`와 `selected_packs`의 비식별자 문자열 길이를 `limit` 이하로 단언한다(`truncation`의 경로와 객체 키는 제외). 그다음 `ontology_get_node`가 원래 본문 전체를 돌려주는지 확인한다. 다음 경우도 확인한다.

- 긴 id 소스(그래프 행 없음): `found=false`.
- `metadata.node_id`가 없는 id를 가리키는 소스: `found=false`.
- `metadata.node_id`가 다른 기존 노드를 가리키는 소스: `found=true`이고 원문이 아니다.
- 기존 그래프 행이 있는 긴 id: 원문을 돌려준다.

### 역변이 시험

다음 변이를 각각 넣어 각자 시험이 실패하는지 본다: 항목 상한 호출 전체 삭제, 구성원 처리 삭제, `graph_context` 처리 삭제, `selected_packs` 처리 삭제, 예산 검사 삭제, 이분 탐색을 고정 `k`로 대체. 절차는 `docs/gate-recipes.md` 회귀 대사 절의 역변이 소절을 따른다.

## 받아들임 기준

- 항목 상한: 결과 항목의 `text`, `metadata`, `graph_context`(식별자 키 제외)에 있는 문자열 값이 `limit` 이하다(접힌 구성원 포함). `selected_packs[i]`의 `pack_id` 외 문자열 값이 `limit` 이하다. 객체 키와 `truncation.fields[].path`는 이 기준 밖이다.
- 잘린 문자열은 `truncation`에 위치와 원래 길이를 가진다. 잘린 문자열이 없는 항목은 입력 항목과 바이트 단위로 일치한다. 응답 단위의 일치는 결정표 행 A에서만 성립한다.
- 식별자 자리는 입력 값을 유지한다.
- 응답 예산: 행 A와 행 B의 응답은 `L`이 `B` 이하다. 행 B의 응답은 앞쪽 결과를 순서대로 담고 `results_truncated`와 `omitted_results`를 싣고, `total`은 담은 결과 수다. 행 C에서만 고정 오류 응답을 돌려준다.
- `ontology_get_node`가 실제 저장소의 표준 `TextUnit` 노드에서 원래 본문 전체를 돌려준다. 돌려주지 못하는 조건은 문서에 적혀 있다.
- 기존 키는 이름과 의미를 유지한다. 새 키는 항목과 선택 팩의 `truncation`, 최상위의 `results_truncated`와 `omitted_results`다. `total`의 의미는 반환한 결과 수다.
- 계약 변경이 `docs/ARCHITECTURE.md`와 도구 설명에 적혀 있다.
- 새 시험이 구현을 되돌리면 실패하고 기존 시험과 린트가 통과한다.

## 범위 밖

- 총량을 맞추는 자동 `limit` 조정. 덜어 낸 결과를 다음 호출로 이어서 받는 방법(페이지 넘기기).
- CLI `query`와 REST `/api/query`의 상한과 예산.
- 상한과 예산의 설정화.
- `ontology_get_node`가 큰 본문을 쪼개 돌려주는 방법.
- 그래프 노드가 없는 소스와 `metadata.node_id`가 다른 소스의 전체 본문을 MCP로 읽는 방법.
- 전송 직렬화의 `ensure_ascii=False` 전환. 글자당 증가량의 측정값은 PR 본문의 후속 칸에 적는다.
- `LocalDocStore.upsert_source`가 본문을 4096자로 줄여 저장하는 기존 동작(이 저장소는 FTS를 지원하지 않아 FTS leg 표에 해당하지 않는다).
