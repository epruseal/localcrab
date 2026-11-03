# #568 REST 질의에 문서 저장소 연결 (#545 흡수)

이 문서는 #568의 설계다. 살아 있는 문서이므로 관측 수치를 적지 않는다. 재현 명령은 8절에 있다.

## 1. 결론

`HybridQuery`가 문서 저장소를 받는 자리를 생성자 인자 `doc_store`로 만든다. 변경은 넷이다.

1. REST 컨텍스트 생성(`apps/api/main.py`의 `_build_context`)과 MCP 컨텍스트 생성(`opencrab/mcp/tools/__init__.py`의 `_assemble_context`)이 같은 인자로 문서 저장소를 넘긴다. 이 연결이 BM25 레그, FTS 레그, 원문 출처(source locator) 증명을 REST에서 켠다.
2. REST `query_ontology`가 `resolve_packs`에 `hybrid`와 `spaces`를 넘긴다. MCP `ontology_query`와 CLI `query`가 이미 넘기는 인자다. 이 인자가 없으면 `auto_pack` 내용 탐침(#400)이 REST에서 꺼져 문서 저장소를 연결해도 REST와 MCP의 팩 선택이 갈린다.
3. REST `_close_context`가 저장소를 닫기 전에 BM25 워커를 멈추고 끝나길 기다린다(3.3절). 문서 저장소를 연결하면 REST 적재가 BM25 재빌드 워커를 시작하기 때문이다(2.2절).
4. MCP의 종전 속성 대입은 같은 인자로 바꾼다.

#545는 같은 근본원인(질의 객체에 문서 저장소가 없음)이므로 이 PR이 흡수한다. #545의 둘째 기준(range 조회 경로)은 9절에서 안내 문서로 닫는다.

## 2. 근본원인

`HybridQuery`는 문서 저장소를 `self._doc_store`에 둔다. 생성자는 이 값을 `None`으로 두고(`opencrab/ontology/query.py`의 `HybridQuery.__init__`), 호출자가 생성 뒤에 속성을 대입해야 한다. 이 값이 `None`이면 `HybridQuery.query`의 세 단계가 건너뛰어진다.

| 단계 | `None`일 때 | 근거 |
|---|---|---|
| BM25 레그 | 건너뜀 | `query`의 `use_bm25 and self._doc_store is not None` 조건 |
| FTS 레그 | 빈 목록 | `_fts_search`의 `ds is None` 반환 |
| 원문 출처 증명 | 증명 없음 | `_source_provenance(self._doc_store, ...)`와 `_add_provenance`가 저장소를 받지 못한다 |

연결을 대입하는 곳은 질의 표면마다 따로 있다. 그래서 한 표면에서 빠져도 다른 표면의 시험이 잡지 못한다.

### 2.1 호출자 전량 (HybridQuery 생성 지점, 현재 main에서 `git grep "HybridQuery("`로 센 결과)

| 위치 | 용도 | 문서 저장소 연결 |
|---|---|---|
| `apps/api/main.py` `_build_context` | REST 질의, 적재 | 없음 (이 결함) |
| `opencrab/mcp/tools/__init__.py` `_assemble_context` | MCP 질의, 적재 | 생성 뒤 대입 |
| `opencrab/cli.py` 질의 명령 | CLI 질의 | 생성 뒤 `docs.available`일 때 대입 |
| `opencrab/cli.py` 적재 명령 | CLI 적재만 | 없음. 질의를 부르지 않는다 |
| `scripts/seed_ontology.py` | 적재만 | 없음. 질의를 부르지 않는다 |
| `scripts/search_baseline.py`, `scripts/search_faults.py` | 합성 하네스 | 하네스가 자기 저장소를 연결한다. 이 변경이 건드리지 않는다 |

질의를 부르는 제품 진입점은 REST, MCP, CLI 셋이다. 이 중 REST만 연결이 없다.

### 2.2 REST 안의 `ctx.hybrid` 사용처와 연결이 바꾸는 것

| 사용처 | 문서 저장소를 읽는가 | 연결 뒤 변화 |
|---|---|---|
| `query` | 읽는다 | BM25, FTS, 증명 단계가 켜진다 |
| `keyword_search` | 읽지 않는다(그래프 저장소만) | 없음 |
| 적재: `write_source`가 `hybrid.ingest`를 부르고 `ingest`가 끝에서 `invalidate_bm25_cache`를 부른다 | 읽는다 | 아래 |

`invalidate_bm25_cache`는 `_node_index_store()`로 문서 저장소를 읽는다. DB 노드 색인이 서비스하면 메모리 무효화를 건너뛰고 기록만 남긴다. 아니면 `_Bm25CacheWorker.invalidate`가 돈다. `invalidate`는 문서 저장소 getter가 `None`이면 `dirty`만 세우고 끝난다. 연결 뒤에는 워커 스레드(`bm25-rebuild`, 데몬)를 시작하고 깨운다. 그래서 연결 전에는 REST 프로세스에 없던 백그라운드 스레드가 REST 적재와 질의 경로에서 생긴다. 이 스레드는 문서 저장소를 읽는다.

REST 종료 경로(`_close_context`)는 저장소를 닫기만 하고 워커를 멈추지 않는다. 워커가 닫힌 저장소를 읽으면 `_rebuild_loop`의 `except Exception`이 경고 로그로 삼킨다. 그래도 종료 순서가 정해지지 않은 채 스레드가 저장소를 읽는 것은 이 연결이 새로 여는 경로다. 3.3절이 닫는다.

## 3. 수정

### 3.1 생성자 인자

1. `HybridQuery.__init__`에 키워드 인자 `doc_store: Any = None`을 더한다. 값은 `self._doc_store`에 둔다. 인자를 생략한 호출자(합성 하네스, 시험, 적재 전용 호출자)는 종전과 같다.
2. REST `_build_context`는 `HybridQuery(vector, graph, doc_store=docs)`로 만든다.
3. MCP `_assemble_context`는 `HybridQuery(vector, graph, doc_store=docs)`로 만들고 종전의 `hybrid._doc_store = docs` 대입을 지운다. `hybrid._rebac = rebac` 대입은 그대로 둔다.
4. CLI 질의 명령은 바꾸지 않는다. 이미 연결하며, 연결 조건이 `docs.available`이라 이 변경이 의미를 바꾸지 않는다.

문서 저장소는 `docs`(`make_doc_store`가 만든 객체) 하나이며 REST 컨텍스트의 `ctx.docs`와 같은 객체다. 새 객체를 만들지 않는다.

### 3.2 자동 팩 선택

REST `query_ontology`의 `resolve_packs` 호출에 `hybrid=ctx.hybrid, spaces=payload.spaces`를 더한다. MCP는 `hybrid=ctx["hybrid"], spaces=spaces`를, CLI는 `hybrid=hybrid, spaces=space_filter`를 넘긴다(각 호출 지점의 인자 목록). `resolve_packs`는 `auto_pack`이 켜져 있고 `choose_packs`(제목, 설명, 키워드, 태그의 리터럴 토큰)가 후보를 못 찾았을 때만 `hybrid`를 쓴다(`opencrab/services/pack_selection.py`의 `if not candidates and hybrid is not None`). 그 경우 `_choose_by_content_with_warnings`가 후보 팩마다 BM25와 FTS를 탐침한다. 문서 저장소가 연결되지 않은 `hybrid`로는 이 탐침이 아무것도 찾지 못했다. 그래서 REST는 `hybrid`를 넘겨도 효과가 없었고 누락이 드러나지 않았다. 연결 뒤에는 REST만 이 인자가 없어 `auto_pack` 요청의 팩 선택이 MCP와 다르다(읽기 범위 A와 B에서 A의 본문만 질의어와 맞으면 MCP는 A를, REST는 범위 전체를 검색한다). 이것은 받아들임 기준 "같은 질의에서 같은 레그 구성"의 일부이므로 이 PR에 넣는다.

`auto_pack`이 꺼진 요청(기본값)에서는 이 인자가 읽히지 않는다.

### 3.3 REST 종료 순서

`_close_context`는 저장소 정리 루프 앞에서 `_stop_bm25_worker`(`apps/api/main.py`)를 불러 다음 순서로 워커를 멈춘다. 예외는 저장소 정리와 같은 방식으로 삼키고 디버그 로그를 남긴다.

1. `hybrid.shutdown_bm25()`를 부른다. 정지 신호를 세우고 워커를 기본 대기(`_Bm25CacheWorker.shutdown`의 2초)까지 기다린다.
2. 그 뒤에도 워커 스레드(`hybrid._bm25_worker`)가 살아 있으면 경고 로그를 남기고 `hybrid._bm25_join()`을 시간 제한 없이 부른다. `_bm25_join`은 "tests/shutdown"에서 진행 중인 재빌드가 정리되길 기다리는 용도로 `HybridQuery`에 이미 있다. 정지 신호가 세워졌으므로 워커는 진행 중인 회차를 끝내고 끝난다.
3. 워커가 끝난 뒤에 기존 정리 루프가 저장소를 닫는다.

이 계약에서 저장소는 워커가 끝난 뒤에만 닫힌다. 종료 시간은 진행 중인 빌드의 완료에 종속된다. 빌드의 크기는 `OPENCRAB_BM25_NODE_LIMIT`와 `OPENCRAB_BM25_CHAR_BUDGET`이 묶는다(4.2절).

워커가 시작하는 조건은 7절에 있다. 기본 배치(SQLite 문서 저장소에 노드 색인이 준비됨)에서는 `invalidate_bm25_cache`가 워커 시작을 건너뛰므로 REST 적재가 워커를 시작하지 않는다. 워커는 메모리 경로(PostgreSQL, MongoDB, 노드 색인 미준비, 킬 스위치)에서 시작한다. `_rebuild_loop`는 노드 색인 준비 상태를 검사하지 않는다. 이미 시작한 워커는 저장소 종류와 무관하게 `_observe`를 통해 그 저장소의 읽기 메서드(SQLite, `PgDocStore`, `MongoStore`)를 부른다. 이 읽기가 돌아오지 않으면 종료가 멈춘다. 이 설계는 그 읽기가 끝난다고 가정한다(10절). 멈추면 종료는 외부가 프로세스를 끊을 때까지 진행하지 않는다. `apps/api/main.py`의 직접 실행 진입점(`uvicorn.run`)은 정지 제한 시간을 설정하지 않는다.

`opencrab/stores/_sqlite_base.py`의 `_SqliteConnMixin`은 `_conn`과 `_new_conn`이 종료 상태를 검사하지 않는다. 이 계약 아래에서는 워커가 이 속성에 도달하지 않는다. 속성 자체는 이 PR에서 고치지 않고 9절에 적는다. MCP 종료 경로에는 같은 정리 호출이 없다. 그 경로도 9절에 적는다.

### 3.4 연결하지 않는 것: ReBAC

MCP는 `hybrid._rebac = rebac`도 대입한다. REST는 `ReBACEngine`을 만들지 않으며 `query`에 `subject_id`를 넘기지 않는다. `_rebac`은 `subject_id`가 있을 때만 읽힌다(`query`의 정책 필터 단계). MCP도 `subject_id`를 넘기지 않는다(`opencrab/mcp/tools/query.py` 주석과 호출). 두 표면에서 이 값은 읽히지 않으므로 REST에 더하지 않는다.

## 4. 동작 변화와 운영 영향

REST `/api/query`에서 다음이 바뀐다.

- BM25 레그와 FTS 레그가 결과를 낸다. MCP와 같은 질의에서 같은 레그 구성이 된다.
- 잘린 본문의 결과 항목이 `truncation.source_locator`를 받을 수 있다. 조건은 MCP와 같다(`docs/ARCHITECTURE.md` 11절). 종전에는 `full_text_unavailable`만 받았다.
- `auto_pack` 요청의 팩 선택이 MCP와 같아진다(3.2절).
- BM25 커버리지 경고가 응답의 `spaces_filter_warnings`에 실릴 수 있다. 이 필드는 `outcome.warnings`를 그대로 싣는다.

바뀌지 않는 것: 응답 형상, 바이트 예산 계약(`result_caps.apply_response_budget`), 인증, 읽기 범위. 코드가 `write.lock`을 잡는 경로도 없다.

### 4.1 SQLite 노드 색인 경로의 검색 중 쓰기

SQLite 문서 저장소는 노드 색인이 준비돼 있으면 그 색인으로 BM25를 서비스한다(`HybridQuery._node_index_store`, 킬 스위치 `OPENCRAB_BM25_DB_INDEX=0`). 이 경로의 `LocalSQLDocStore.node_bm25_search`는 먼저 `_node_bm25_try_drain`을 부른다. 대기 중인 키가 있으면 이 함수가 저장소 내부 락을 비차단으로 얻고(`acquire(blocking=False)`) 배치마다 `BEGIN IMMEDIATE`로 색인을 갱신해 커밋한다. `busy_timeout`은 짧게 줄인다. 시간 예산(`_NODE_BM25_DRAIN_SECONDS`)은 배치를 시작하기 전에만 확인한다. `drain` 안에서는 확인하지 않으므로 느린 배치 하나는 예산을 넘겨 쓰기 잠금을 더 오래 잡을 수 있다. 예산은 배치 사이의 상한이고 전체 실행 시간의 엄격한 상한이 아니다. 락이나 DB가 바쁘거나 읽기 전용이면 예외를 삼키고 건너뛴다(디버그 로그). 건너뛰면 색인에 대기 키가 남고 BM25 레그가 `BM25 coverage pending changes` 경고를 낼 수 있다(`docs/issue-541-db-bm25.md`). 이 동작은 MCP 질의가 이미 하는 것이다. REST 연결이 이 경로를 REST 프로세스에서도 열므로 SQLite 쓰기 잠금이 REST 질의 중에 잠깐 잡힐 수 있다. 프로세스 사이 충돌은 SQLite의 잠금과 `busy_timeout`이 처리하고, 진 쪽은 건너뛴다. 시험은 대기 키가 있는 색인에서 REST 질의가 성공하는지 확인한다(5절).

### 4.2 메모리 영향

메모리 경로(PostgreSQL, MongoDB, 노드 색인 미준비, 킬 스위치)에서는 REST 프로세스가 MCP 프로세스와 별도로 BM25 색인을 가진다. 색인의 상한은 기존 설정이 정한다: 노드 수 `OPENCRAB_BM25_NODE_LIMIT`, 저장 문자열 창 `OPENCRAB_BM25_CHAR_BUDGET`, 노드별 길이 `OPENCRAB_BM25_MAX_NODE_CHARS`(`docs/ARCHITECTURE.md` 6절). 이 설정은 색인 하나의 크기를 묶는다. 프로세스 전체 메모리 상한은 아니고, 재빌드 동안 기존 색인과 새 후보가 함께 있는 구간이 있다(#506, `docs/issue-506-bm25-memory.md`). 연결 뒤 운영자는 REST 프로세스 수만큼 이 비용을 더 진다. SQLite 노드 색인이 준비된 배치(기본)에서는 메모리 색인을 빌드하지 않는다.

### 4.3 #302 도달 가능성 판정의 갱신

`tests/test_bm25_cold_build_race.py`의 `test_rest_context_never_attaches_a_doc_store_to_hybrid_query`는 "REST는 BM25에 닿지 않는다"는 #302 시점의 판정을 소스 문자열 검사로 고정했다. 이 판정은 이 변경으로 끝난다. 새 연결 방식(`doc_store=docs`)에서는 문자열 `_doc_store =`가 없어 이 시험이 계속 통과하지만 내용은 거짓이 된다(설계 검증에서 새 생성자 호출 문자열에 대해 통과함을 확인했다). 그래서 같은 변경에서 이 시험을 지우고 5절의 연결 시험이 대신한다.

#302가 요구한 안전장치는 코드에 있다. 콜드 빌드는 `_Bm25CacheWorker.ensure_built`의 전용 락으로 직렬화된다(`docs/ARCHITECTURE.md` 1절의 BM25 설명). SQLite 문서 저장소는 연결을 스레드별로 쓰므로 REST 스레드풀이 연결을 공유하지 않는다. 락 시험(`tests/test_bm25_cold_build_race.py`의 나머지)은 `HybridQuery` 수준이어서 REST 연결과 무관하게 같은 보장을 시험한다.

### 4.4 동등성의 범위

"같은 질의에서 같은 레그 구성"은 두 핸들러가 같은 값을 받는 입력에서 성립한다. 소스 대조 결과는 다음과 같다.

- 같은 입력: `question`, `spaces`, `limit`(기본값 10), `pack_ids`, `auto_pack`, `include_unpackaged`. 레그 선택 `use_bm25`, `use_fts`, `use_rerank`는 REST 요청 모델(`QueryRequest`)에 없고 `query_ontology`가 넘기지 않는다. REST는 `HybridQuery.query`의 기본값(셋 모두 참)을 쓴다. MCP는 같은 기본값(참)을 인자로 받아 넘긴다. 그래서 기본값 호출에서 두 표면은 같고, 레그를 끄는 MCP 호출은 REST에 대응이 없다. REST에 인자를 더하는 일은 기능 추가이므로 하지 않는다.
- 다른 입력: `graph_depth`는 REST 요청 모델에만 있고(기본 1) 핸들러가 `query`에 넘긴다. MCP는 넘기지 않아 `query`의 기본값(1)을 쓴다. 기본값에서 같다. `offset`은 양쪽에 있고 응답 자르기에만 쓰인다.
- REST에만 있는 응답 필드: `keyword_fallback`(기본 검색이 비었을 때 그래프 키워드 검색). 이 필드는 `_doc_store`를 읽지 않으며 레그 구성 비교에서 뺀다.

시험은 이 범위를 따른다. 기본값 입력에서 두 핸들러의 (노드, 레그) 목록과 팩 선택을 비교한다. 레그 선택 인자와 `graph_depth` 변형은 비교하지 않는다.

## 5. 시험

새 파일 `tests/test_rest_doc_store_568.py`. 실제 SQLite 저장소와 sqlite-vec 벡터 저장소를 쓴다. REST 컨텍스트는 `apps.api.main._build_context`를 실제로 불러 만든다(저장소 팩토리만 같은 저장소를 돌려주게 바꾼다). MCP 컨텍스트는 `_assemble_context`를 실제로 불러 만든다. 질의는 REST 핸들러 `query_ontology`와 MCP 핸들러 `ontology_query`를 직접 부른다. 두 핸들러에 같은 주체와 같은 입력을 준다.

| 시험 | 확인 | 연결을 지운 역변이에서 |
|---|---|---|
| REST 컨텍스트의 문서 저장소가 `ctx.docs`와 같은 객체다 | 연결 | 실패 |
| REST와 MCP 컨텍스트가 같은 저장소 객체를 문서 저장소로 쓴다 | 두 경로 동일 연결 | 실패 |
| REST `query_ontology`가 BM25 전용 노드를 `source == "bm25"`로 돌려준다 (벡터 저장소는 그 노드를 돌려주지 않는다) | BM25 레그 결과 | 실패 |
| 같은 질의에서 REST 핸들러와 MCP 핸들러의 (노드, 레그) 목록이 같다 | 같은 레그 구성 | 실패 |
| `auto_pack` 요청에서 REST와 MCP의 `selected_packs`와 `pack_filter.pack_ids`가 같다. 팩 메타데이터는 질의어와 맞지 않고 한 팩의 본문만 맞는다 | 팩 선택 | `doc_store` 지우기와 `hybrid` 인자 지우기 각각에서 실패 |
| 잘린 본문 항목이 `source_locator`를 받는다. 대조군은 연결 없는 컨텍스트에서 `full_text_unavailable`이다 (#545) | 원문 출처 | 실패 |
| (새 시험 없음) 대기 키가 있는 노드 색인의 검색은 기존 시험 `tests/test_node_bm25_index.py`가 저장소 수준에서 덮는다(4.1절의 비차단 락, 건너뜀, 경고). REST 연결은 같은 저장소 메서드를 부를 뿐이다 | 4.1절 | 해당 없음 |
| 종료 순서. 메모리 경로를 강제하고(`OPENCRAB_BM25_DB_INDEX=0`) 워커의 저장소 읽기 호출(`_observe`가 부르는 `bm25_fingerprint`와 `list_nodes_within_budget`)을 이벤트로 막아 워커가 빌드 중임을 확정한다. `hybrid._bm25_join`에 감시 래퍼를 둔다. 래퍼는 호출 인자를 기록하고 진입 신호를 보낸다. 그 상태에서 `_close_context`를 별도 스레드로 부르면 (a) 호출이 끝나지 않고 (b) 저장소 `close`가 불리지 않는다. 시험은 감시 래퍼의 진입 신호를 기다린 뒤에만 이벤트를 푼다. 즉 기본 대기(2초)가 지나 두 번째 대기에 들어간 상태에서 풀린다. 신호가 시간 안에 오지 않으면 시험은 실패한다. 시험은 기록된 `_bm25_join`의 `timeout` 인자가 `None`(시간 제한 없음)임을 단언한다. 이벤트를 풀면 호출이 끝나고, `close`가 불린 시점에 워커 스레드가 이미 끝나 있었음을 단언한다. 대조군: 워커를 시작하지 않은 컨텍스트에서는 `_close_context`가 막히지 않는다 | 3.3절 | 정리 두 단계를 모두 지운 역변이, 두 번째 단계(`_bm25_join`)만 지운 역변이, 두 번째 단계에 유한한 `timeout`을 넣은 역변이, 저장소를 먼저 닫도록 순서를 바꾼 역변이 각각에서 실패 |

대조군: 문서 저장소를 연결하지 않은 `HybridQuery`는 같은 질의에서 BM25 항목을 내지 않는다. 이 대조군이 "BM25 항목이 있다"는 단언이 우연히 참인 경우를 막는다. FTS나 그래프와 합쳐진 항목(`source == "hybrid"`)은 BM25 참여의 증거로 쓰지 않는다.

역변이는 `docs/gate-recipes.md` 6절 역변이 소절을 따른다. 대상은 REST `_build_context`의 `doc_store=docs`, `query_ontology`의 `hybrid=ctx.hybrid`, `_close_context`의 워커 정지 두 단계 셋이다. 각각 지워 해당 시험의 실패(RED)를 확인한 뒤 되돌린다.

## 6. SOLID 판정

- 단일 책임: `__init__`이 인자 하나를 대입한다. 새 단위를 만들지 않는다. `_close_context`는 이미 자원 정리를 맡은 함수이고 정리 항목 하나를 더한다.
- 개방 폐쇄: 해당 없음. 분기를 더하지 않는다.
- 리스코프 치환: 인자를 생략한 호출자(`HybridQuery(a, b)`)의 동작이 같다. 속성 대입 방식(`hybrid._doc_store = x`)도 그대로 동작한다. 시험과 하네스 호출자가 이를 쓴다.
- 인터페이스 분리: 해당 없음.
- 의존 역전: 문서 저장소를 생성자 인자로 받는다. 상위 절차가 구현을 직접 만들지 않는다. 별도 인터페이스 타입은 만들지 않는다(바꿔 끼울 필요가 시험 대역뿐이다).

## 7. 상태 전이와 자원 위치

이 변경은 REST 프로세스에 워커 스레드 수명이라는 상태 전이를 더한다.

| 단계 | 워커 | 문서 저장소 |
|---|---|---|
| 컨텍스트 생성 | 없음. `HybridQuery` 생성자는 스레드를 만들지 않는다 | 열림 |
| 메모리 경로의 첫 질의 | 시작하지 않는다. `_bm25_acquire_state`가 `ensure_built`로 질의 스레드에서 동기 빌드한다 | 열림 |
| 첫 `invalidate`(REST 적재가 `invalidate_bm25_cache`를 부름) 또는 `mark_stale`(질의 중 지문 불일치 재감지) | 시작(데몬). DB 노드 색인이 서비스하면 `invalidate_bm25_cache`는 시작을 건너뛴다 | 열림 |
| 종료(`_close_context`) | 정지 신호, 기본 대기 뒤에도 살아 있으면 끝날 때까지 대기 | 워커가 끝난 뒤 닫힘 |

중단 후 재개는 해당 없다. 워커는 프로세스 수명 안의 캐시를 만들 뿐 파일이나 레코드에 상태를 남기지 않는다. 프로세스가 죽으면 다음 기동이 처음부터 시작한다. 노드 색인의 대기 키는 문서 저장소 파일에 있고 재기동 뒤 기존 드레인 경로가 이어받는다.

## 8. 재현 명령

```bash
.venv/bin/pytest tests/test_rest_doc_store_568.py -q
git grep -n "HybridQuery(" -- opencrab apps scripts
git grep -n "resolve_packs(" -- apps opencrab
```

회귀 대사와 린트는 `docs/gate-recipes.md`를 따른다.

## 9. 범위 밖과 후속 후보

- REST range 조회 경로. #545의 둘째 기준은 "range 조회 경로(REST 또는 안내)"를 요구한다. 이 변경은 안내로 닫는다. `docs/ARCHITECTURE.md` 11절에 REST 응답의 `source_locator`를 읽으려면 같은 원문 저장소와 해당 팩의 읽기 권한이 필요하고, 읽는 길이 MCP 도구 `ontology_get_source_range`(응답의 `full_text_tool`)뿐이며 REST에는 해당 경로가 없다고 적는다. REST 경로를 새로 만드는 일은 범위 확장이므로 이 변경에 넣지 않는다. 사용자가 원하면 별도 이슈로 다룬다.
- MCP 종료 경로의 `shutdown_bm25`. MCP도 종료 때 워커를 멈추지 않는다. 이 변경이 새로 만든 경로가 아니며 후속 후보로 올린다.
- `_SqliteConnMixin`의 닫힌 뒤 연결 재생성. `_conn`과 `_new_conn`이 종료 상태를 검사하지 않아 닫힌 저장소에서 다른 스레드가 연결을 새로 열 수 있다. 모든 SQLite 저장소에 걸치는 별도 변경이므로 후속 후보로 올린다.
- REST 요청 모델에 `use_bm25`, `use_fts`, `use_rerank`를 더하는 일. 4.4절의 동등성 범위 밖이다.

## 10. 가정

- REST와 MCP 프로세스가 같은 설정으로 같은 `doc_store.db` 파일을 연다(운영 배치). 라이브에 접근해 확인하지 않았다. 틀리면 "같은 질의에 같은 결과" 주장은 프로세스 사이가 아니라 같은 저장소 기준으로만 성립한다.
- `make_doc_store`가 돌려주는 객체는 BM25 읽기 메서드(`list_nodes`, 선택적으로 `bm25_fingerprint`, `node_bm25_*`)를 갖는다. 갖지 못하면 BM25 레그가 예외를 경고 로그로 삼키고 빈 결과를 낸다(`_bm25_search_state`의 `except`). 질의 자체는 실패하지 않는다.
- 시작한 워커의 저장소 읽기(SQLite, PostgreSQL, MongoDB)는 끝난다. 이 가정이 틀리면 종료가 외부가 프로세스를 끊을 때까지 멈춘다(3.3절). 소스에서 읽기 하나가 영원히 돌아오지 않는 경로를 확인하지는 않았다.
