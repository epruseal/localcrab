# #541 노드 BM25의 DB 전량 색인

이 문서는 #541의 설계와 운영 절차를 정한다. 앞부분은 설계이고 마지막 두 절은 운영 절차와 실행 가이드다. 살아 있는 문서이므로 관측 수치를 적지 않는다. 착수 전 probe와 전환 대조의 수치는 날짜를 박은 기록(`docs/records/issue-541-db-bm25-2026-10-25.md`)과 PR 본문에 둔다.

## 1. 결론

노드 BM25 색인을 `doc_store.db` 안의 FTS5 contentless 표로 옮긴다. 표에는 기존 `text_cues.tokenize`가 낸 토큰 열을 그대로 저장한다. 점수는 Python이 기존 `BM25Index`와 같은 식으로 계산한다. 그래서 바뀌는 것은 색인 범위뿐이다. 전역 최근 갱신순 창(`OPENCRAB_BM25_NODE_LIMIT`, `OPENCRAB_BM25_CHAR_BUDGET`)이 사라지고 `doc_nodes` 전량이 검색 대상이 된다. 토큰화, 길이 정규화, 순위 식, 동점 순서는 기존과 같다. 메모리 캐시는 두지 않는다(7절).

이 색인은 SQLite 문서 저장소(`LocalSQLDocStore`, local과 kuzu 모드)에만 둔다. PostgreSQL과 MongoDB 문서 저장소는 기존 메모리 창 경로와 그 coverage 경고를 그대로 쓴다(10절).

## 2. 현재 경로와 근본원인 (소스 근거)

- `HybridQuery._bm25_search_state`가 `_Bm25CacheWorker.state`의 `BM25Index`를 검색한다. 상태는 `_observe`가 `list_nodes_within_budget(limit=_BM25_NODE_LIMIT, max_chars=...)`로 읽은 창이다. 창은 `doc_nodes`를 `updated_at DESC, space, node_id` 순으로 자른다.
- `BM25Index.build`는 창의 노드마다 `tokenize(_node_text(n)[:max_text_chars])`를 만든다. 문서 빈도(df), 문서 수(N), 평균 길이(avgdl)도 창 안에서만 센다. `search`는 요청 팩 버킷의 모든 문서를 훑어 점수를 매긴다.
- 근본원인은 색인 범위가 전역 창이라는 점이다. 창 밖 문서는 어떤 팩을 요청해도 BM25가 찾지 못한다. #398은 이 사실을 경고로 알리게 했지만 누락 자체는 남겼다. #542 기준선의 축소 상한 설정에서 BM25 누락과 자동 팩 오선택이 이 원인으로 재현됐다.
- 부수 비용도 같은 구조에서 나온다. 첫 질의가 창 전체를 읽고 토큰화하는 동안 기다린다. 질의마다 전역 `COUNT(*)`와 `MAX(updated_at)` 지문을 읽는다. 쓰기 뒤에는 디바운스 뒤 전역 재구축을 기다려야 결과에 반영된다.

## 3. 저장 방식 비교

이슈의 첫 작업 항목은 SQLite FTS5의 노드 문서 확장과 다른 기존 저장소의 검색 능력을 비교하는 것이다. 후보와 판정은 다음과 같다. 크기와 비용의 관측값은 날짜 기록의 probe 절에 있다.

| 후보 | 판정 | 사유 |
|---|---|---|
| 기존 `doc_sources_fts`처럼 `unicode61` FTS5로 노드 본문 색인, `bm25()` 순위 | 채택 안 함 | 토큰화가 다르다. `unicode61`은 한글 n-gram을 만들지 않고 `_`에서 끊는다. FTS5 `bm25()`는 k1과 idf 식이 기존과 다르다. 순위와 한국어 재현율이 함께 바뀌므로 노드 검색 계약을 보존했다고 볼 수 없다. |
| 맞춤 역색인 표 `(term, pack, doc, tf)` | 채택 안 함 | 점수는 정확히 같게 만들 수 있다. 그러나 행마다 B-tree 오버헤드가 붙어 디스크가 노드 검색 본문의 여러 배가 된다. 4.5절 디스크 요구치(색인이 원문 본문의 2배 이하)를 넘는다. |
| FTS5 contentless 표에 기존 토큰 열을 저장, 점수는 Python | 채택 | 토큰 열을 `ascii` 토크나이저(`tokenchars '_'`)로 넣으면 FTS5가 공백에서만 끊으므로 저장 토큰이 Python 토큰과 같다(4.3절). df는 `fts5vocab` `row` 표, tf는 `instance` 표에서 읽는다. 점수는 기존 식으로 계산하므로 같다. FTS5 doclist는 varint 압축이라 디스크가 작다. |
| PostgreSQL `tsvector`나 `pg_trgm` | 이 이슈에서 구현 안 함 | local 모드에 없다. 토큰화와 순위가 다르다. 10절의 후속 후보다. |
| Neo4j 전문 색인(Lucene) | 채택 안 함 | 그래프 저장소이고 docker 모드에만 있다. BM25 레그는 문서 저장소만 읽는다. 분석기가 다르다. |
| 새 의존성(외부 검색 엔진, Rust 확장 등) | 채택 안 함 | 위 채택안이 표준 SQLite만으로 정확성과 크기 요구를 함께 충족한다. 비용을 정당화할 근거가 없다. |

채택안은 SQLite 3.43 이상의 `contentless_delete=1` 옵션에 기댄다. 옵션이 없는 빌드에서는 색인 DDL이 실패하고 저장소는 색인을 `unsupported`로 두고 기존 경로를 쓴다(5.2절).

## 4. 설계

### 4.1 스키마 (`doc_store.db`, SQLite)

모든 이름은 `doc_nodes_bm25` 접두를 쓴다.

| 객체 | 정의 | 역할 |
|---|---|---|
| `doc_nodes_bm25_fts` | `fts5(tokens, content='', contentless_delete=1, tokenize="ascii tokenchars '_'")` | 문서별 토큰 열. `rowid`가 `doc_key`다. |
| `doc_nodes_bm25_vrow` | `fts5vocab(doc_nodes_bm25_fts, 'row')` | 용어별 문서 빈도. 저장 공간 없음. |
| `doc_nodes_bm25_vinst` | `fts5vocab(doc_nodes_bm25_fts, 'instance')` | 용어의 출현 위치. tf를 센다. 저장 공간 없음. |
| `doc_nodes_bm25_docs` | `doc_key INTEGER PRIMARY KEY, space, node_id, pack_id, dl, updated_at, UNIQUE(space, node_id)` | 색인 문서의 신원, 범위 키, 토큰 수, 동점 순서 키. `pack_id`는 `scope_pack_id`의 값이고 없으면 빈 문자열이다. |
| `doc_nodes_bm25_stats` | `id=1, doc_count, total_len` | N과 토큰 총수. 문서 색인 변경과 같은 트랜잭션에서 갱신한다. |
| `doc_nodes_bm25_pending` | `(space, node_id) PRIMARY KEY, WITHOUT ROWID` | 아직 색인에 반영하지 않은 `doc_nodes` 변경의 키. |
| `doc_nodes_bm25_meta` | `key, value` | `format`, `max_text_chars`, `state`(`backfill` 또는 `ready`), backfill 커서 `cursor_space`와 `cursor_node_id`. |
| 트리거 셋 | `doc_nodes`의 `AFTER INSERT`, `AFTER UPDATE`, `AFTER DELETE` | 바뀐 행의 키(UPDATE는 옛 키와 새 키 모두)를 `pending`에 넣는다. 순수 SQL이다. 넣기는 `INSERT ... SELECT ... WHERE NOT EXISTS` 형태다. 바깥 문장의 충돌 처리(upsert의 `DO UPDATE`, `INSERT OR REPLACE`)가 트리거 안 `INSERT OR IGNORE`의 충돌 처리를 덮어써 같은 키 두 번째 넣기가 실패하는 것을 착수 전 probe에서 확인했기 때문이다. |

### 4.2 색인 항목 하나를 맞추는 함수

`reindex_key(conn, space, node_id)`가 유일한 색인 쓰기 함수다. 쓰기 경로, pending 소진, backfill이 모두 이 함수를 쓴다.

1. `doc_nodes`에서 그 키의 현재 행을 읽는다.
2. `doc_nodes_bm25_docs`에 기존 항목이 있으면 FTS 행(`rowid=doc_key`)과 docs 행을 지우고 stats에서 그 `dl`과 문서 하나를 뺀다.
3. 현재 행이 있으면 저장소의 `_row_to_node`로 노드 dict를 만든다. 토큰은 `tokenize(_node_text(node)[:max_text_chars])`다. docs 행과 FTS 행을 넣고 stats에 더한다. `pack_id`는 `scope_pack_id(node)`다.
4. `pending`에서 그 키를 지운다.

이 함수는 멱등이다. 같은 키를 여러 번 불러도 마지막 상태는 현재 행 하나를 반영한다. 기존 메모리 색인과 같은 함수(`_row_to_node`, `_node_text`, `tokenize`, `scope_pack_id`)를 쓰므로 문자열이 아닌 `pack_id`(문자열로 바꾼 값), 깨진 properties JSON(`properties={}`와 `property_decode_error`), 빈 `pack_id`의 처리가 기존과 같다. 깨진 행과 무소속 행도 색인에 들어가 N과 df에 기여하고, 범위 키가 빈 문자열이라 어떤 검색 결과에도 나오지 않는다. 기존 창도 이런 행을 담았으므로 같다.

### 4.3 토큰 보존

`tokenize`의 토큰은 공백 없는 `\w` 문자열이다. Python의 ASCII `\w`는 영숫자와 `_`뿐이다. `ascii` 토크나이저는 ASCII가 아닌 문자를 모두 토큰 문자로 보고, `tokenchars '_'`로 `_`도 토큰 문자로 둔다. 그래서 공백으로 이은 토큰 열을 FTS5가 다시 나누면 원래 토큰 열과 같다. ASCII 대문자 접기는 이미 소문자인 토큰에 영향이 없다. 시험이 이 왕복을 확인한다(9절).

FTS5는 32768바이트보다 긴 용어를 바이트 단위로 자른다. 잘린 용어는 UTF-8 문자 중간에서 끊길 수 있고, 앞부분이 같은 두 긴 토큰을 한 용어로 합친다. 그래서 UTF-8 길이가 1024바이트를 넘는 토큰은 색인과 질의 양쪽에서 `U+2063` 문자 뒤에 토큰의 SHA-256 16진 값을 붙인 대리 용어로 바꾼다. `U+2063`은 `\w`가 아니므로 `tokenize`가 만드는 실제 토큰과 겹치지 않는다. 대리 용어는 토큰 하나에 하나이므로 tf, df, 문서 길이가 원래 토큰으로 센 값과 같다.

### 4.4 쓰기 동기화

- 저장소 쓰기 메서드(`upsert_node_doc`, `create_node_doc_if_absent`, `delete_node_doc`)는 `doc_nodes` 쓰기와 그 키의 `reindex_key`를 한 트랜잭션에서 한다. `reindex_key`는 SAVEPOINT 안에서 돈다. 색인 갱신이 예외를 내면 SAVEPOINT만 되돌리고 경고를 남긴다. 트리거가 넣은 pending 행은 SAVEPOINT 앞에 들어갔으므로 남는다. 문서 쓰기는 색인 실패로 실패하지 않는다.
- 그 밖의 모든 `doc_nodes` 변경(팩 삭제의 집합 DELETE, 운영 스크립트의 직접 SQL, 다른 프로세스, 이전 코드 버전, sqlite3 CLI)은 트리거가 pending에 남긴다.
- 검색은 pending이 있으면 먼저 소진한다(4.6절). 소진하지 못한 행이 남으면 coverage 경고를 낸다.

이 구조가 기존 무효화 계약(`invalidate_bm25_cache()` 호출)을 대신한다. 색인의 신선도는 호출자가 무효화를 불렀는지에 기대지 않는다. #44처럼 쓰기 뒤 무효화 호출이 빠진 경로도 BM25 레그에서는 다음 검색 전에 반영된다. `invalidate_bm25_cache()`는 색인이 `ready`이면 아무 일도 하지 않는다. 그래서 DB 색인 모드에서는 기존 재구축 스레드가 시작되지 않는다.

외부 쓰기 감지의 지원 경계는 다음과 같다.

- 지원: 트리거가 있는 같은 SQLite 파일에 대한 모든 SQL 쓰기.
- 지원: 백업 복원처럼 DB 파일을 통째로 바꾸는 일. 색인 표와 `doc_nodes`가 같은 파일에 있으므로 복원본의 색인은 복원본의 행과 같은 시점이다. 색인 표가 없는 옛 백업이면 다음 열기에서 `backfill` 상태가 된다.
- 감지 장치만 있는 경우: `doc_nodes` 표를 지우고 다시 만드는 일은 트리거를 함께 지운다. 다음 열기에서 트리거를 다시 만들고, 열기 점검(5.2절)이 행 수 불일치를 보면 `backfill`로 되돌린다. 행 수가 우연히 같으면 이 점검은 놓친다. 이 경우는 `verify` 명령(11절)으로 찾는다. 저장소 안에 이런 표 재생성 코드는 없다.

### 4.5 검색 알고리즘

입력은 기존 `BM25Index.search`와 같다(질의, `spaces`, `limit`, `pack_ids`). 단계는 다음과 같다.

1. `q_tokens = tokenize(question)`. 비었거나 `pack_ids`가 비었거나 `limit <= 0`이면 빈 결과다.
2. pending 소진(4.6절).
3. 하나의 읽기 트랜잭션에서 아래를 읽는다. 한 스냅숏이므로 stats, df, tf가 서로 맞는다.
   - stats의 `doc_count`(N)와 `total_len`. N이 0이면 빈 결과다. `avgdl = total_len / N`이다.
   - 고유 질의 용어의 df(`vrow`). df가 없는 용어는 기존처럼 건너뛴다.
   - 용어마다 `vinst`와 docs를 이어 요청 팩(빈 문자열 제외)과 `spaces` 안의 `(doc_key, tf)`를 얻는다. 후보마다 `dl`, `updated_at`, `space`, `node_id`, `pack_id`를 얻는다.
4. 후보마다 `q_tokens`를 원래 순서(중복 포함)로 돌며 기존과 같은 식으로 점수를 더한다. 식은 `bm25.py`의 공용 함수 하나에 두고 메모리 색인과 DB 색인이 함께 부른다. 그래서 부동소수 연산 순서까지 같다.
5. 점수가 0보다 큰 후보를 `(-score, updated_at DESC, space, node_id)`로 정렬한다. 이 동점 순서는 기존 창의 위치 순서와 같다.
6. 정렬 순서대로 `doc_nodes`의 현재 행을 일괄로 읽어 다시 검사한다. 행이 없거나, 현재 `scope_pack_id`가 요청 팩 밖이거나, 현재 space가 `spaces` 밖이면 버린다. 남은 행으로 `limit`개를 채운다. 결과 항목의 키와 값(`node_id`, `space`, `node_type`, `score`(넷째 자리 반올림), `properties`, `text=_node_text(현재 노드)`, `pack_id`)은 기존과 같다.

6번은 권한 경계의 최종 확인이다. pending이 남은 상태에서 팩이 바뀐 노드나 지워진 노드가 색인에 남아 있어도 결과에 나오지 않는다. 색인이 동기화된 상태에서는 아무것도 버리지 않으므로 결과가 기존 창 전량 색인의 결과와 같다.

자동 팩 선택의 팩별 탐침(`_bm25_search_per_pack`)은 같은 계산을 한 번 하고 팩별로 상위 `limit`개씩 나눈다. 점수 통계가 전역이므로 팩마다 따로 부른 결과와 같다.

공통 용어(거의 모든 문서에 있는 토큰)를 담은 질의는 `vinst`가 그 용어의 모든 출현을 훑는다. 비용은 코퍼스 전체의 출현 수에 비례하고 요청 팩 크기와 무관하다. 기존 경로는 요청 팩의 모든 문서를 훑었다. 정확한 상위 k를 지키면서 이 비용을 줄이는 방법(MaxScore 계열 가지치기)은 S2 이상 측정에서 필요가 확인되면 후속으로 둔다.

### 4.6 pending 소진과 경고 계약

- 검색은 `pending`이 있는지 `EXISTS`로 확인한다. 있으면 저장소 쓰기 트랜잭션(`BEGIN IMMEDIATE`)에서 키 묶음을 `reindex_key`로 처리한다. 묶음 크기와 검색 한 번의 시간 예산은 모듈 상수다. 예산이 끝나면 멈춘다.
- 소진 뒤에도 pending이 남으면 검색 결과와 함께 `BM25 coverage pending changes: <n> node rows are not indexed yet` 경고를 낸다. 소진이 잠금이나 읽기 전용 파일 때문에 실패해도 같은 경고를 낸다.
- DB 색인 질의가 예외를 내면 빈 결과와 `BM25 coverage unavailable: node index query failed` 경고를 낸다. 기존 경로의 예외 삼킴(빈 결과, 경고 없음)은 DB 경로에 옮기지 않는다.
- 색인이 `ready`이고 pending이 비었으면 coverage 경고가 없다. 전량 색인이므로 기존의 `missing requested pack ids`, `partial rows`, `total unknown` 경고 조건이 생기지 않는다.

경고는 모두 `BM25 coverage` 접두를 쓴다. 기존 결합 경로가 BM25 레그 경고를 `QueryOutcome.warnings`로 옮기므로 MCP 응답에도 나온다.

### 4.7 질의당 전역 COUNT/MAX와 전역 재구축의 대체

| 기존 | DB 색인 모드 |
|---|---|
| 질의마다 `bm25_fingerprint()`의 전역 `COUNT(*)`와 `MAX(updated_at)` | 질의마다 `pending`의 `EXISTS` 하나와 stats 행 하나 |
| 쓰기 뒤 디바운스, 휴지, 전역 재구축, 새 상태 게시 | 쓰기 트랜잭션 안의 키 하나 재색인, 외부 쓰기는 pending 소진 |
| 첫 질의의 cold 구축 | 없음 |

기존 경로는 PG, MongoDB, 킬 스위치, `backfill` 상태에서 그대로 쓴다.

## 5. 상태 기계, backfill, 전환, rollback

### 5.1 상태

| 상태 | 뜻 | 검색 경로 |
|---|---|---|
| `unsupported` | 색인 DDL이 실패했거나(옛 SQLite, FTS5 없음) 저장소가 색인을 갖지 않음 | 기존 메모리 창 |
| `backfill` | 색인 표가 있으나 기존 행 전량이 아직 색인되지 않음 | 기존 메모리 창 |
| `ready` | 기존 행 전량을 한 번 색인했고, 그 뒤 변경은 쓰기 동기화와 pending이 맡음 | DB 색인 |

환경 변수 `OPENCRAB_BM25_DB_INDEX=0`은 검색 경로만 기존 메모리 창으로 돌린다(킬 스위치). 색인 유지(트리거와 쓰기 동기화)는 계속된다. 다시 켜면 그동안 쌓인 변경은 이미 반영되어 있다.

### 5.2 열기 때의 처리

저장소 `_init_db`가 기존 FTS 블록 뒤에서, 같은 `write.lock`(`_bootstrap_lock`) 안에서 한다. 전체가 `BEGIN IMMEDIATE` 트랜잭션 하나다. 실패하면 되돌리고 상태를 `unsupported`로 둔다. 문서 저장소 자체는 `available`로 남는다.

1. 객체를 `IF NOT EXISTS`로 만든다. 새로 만들었으면 `doc_nodes`가 비었을 때 `ready`, 아니면 `backfill`(커서 처음)로 둔다.
2. `format` 또는 `max_text_chars`(`OPENCRAB_BM25_MAX_NODE_CHARS`)가 기록과 다르면 색인 행을 모두 지우고 `backfill`(커서 처음)로 되돌린다.
3. `ready`이고 pending이 비었는데 `COUNT(doc_nodes)`가 stats의 `doc_count`와 다르면 `backfill`로 되돌리고 경고를 남긴다(4.4절의 표 재생성 감지).

열기는 기존 행을 토큰화하지 않는다. 서비스 시작이 장시간 전체 구축을 기다리지 않는다.

### 5.3 backfill과 재개

`opencrab packs bm25-index backfill`이 수행한다. 서비스가 자동으로 시작하지 않는다. 운영자가 백업과 승인 뒤 실행한다(12절).

- 한 단계는 `BEGIN IMMEDIATE` 트랜잭션 하나다. 커서 `(cursor_space, cursor_node_id)` 다음 키부터 기본 키 순서로 묶음 하나를 읽는다. 키마다 `reindex_key`를 부르고 커서를 마지막 키로 옮긴다. 묶음이 비면 남은 pending을 소진하고 상태를 `ready`로 바꾼다.
- 자원 위치: 진행 상태는 `doc_nodes_bm25_meta`의 커서와 상태 행 두 곳에만 있다. 색인 행은 `doc_nodes_bm25_docs`, `doc_nodes_bm25_fts`, stats에 있다. 진행 중 변경은 `doc_nodes_bm25_pending`에 있다. 프로세스 메모리에는 묶음 하나의 행만 있다.
- 중단: 단계 도중 프로세스가 죽거나 예외가 나면 SQLite가 그 트랜잭션을 되돌린다. 커서와 색인 행은 단계 시작 전 상태로 남는다. 부분 단계가 남지 않는다.
- 재개: 다시 실행하면 커서를 읽어 다음 키부터 간다. 커서 앞 키라도 그 뒤 바뀌었으면 쓰기 동기화나 pending이 처리한다. 커서 뒤 키를 쓰기 동기화가 미리 색인했어도 `reindex_key`가 멱등이므로 다시 처리해도 같다.
- 동시 실행: 두 프로세스가 동시에 backfill을 돌려도 단계마다 쓰기 잠금 안에서 커서를 읽고 옮기므로 같은 묶음을 두 번 처리하지 않는다.
- 실패 행: 어떤 행이 `reindex_key`에서 계속 예외를 내면 그 단계는 되돌려지고 명령은 그 키를 출력하고 0이 아닌 코드로 끝난다. 그 행을 건너뛰지 않는다. 건너뛰면 조용한 부분 색인이 된다. 운영자가 그 행을 고친 뒤 다시 실행한다.

### 5.4 전환 검증

`opencrab packs bm25-index verify`가 읽기 전용으로 확인한다. 이 명령은 `doc_nodes`를 기본 키 순서로 스트리밍하며 노드마다 토큰을 다시 계산한다.

- docs 행 집합이 `doc_nodes` 키 집합과 같다. 행마다 `pack_id`, `dl`, `updated_at`이 같다.
- stats가 docs의 합과 같다.
- 표본 용어(노드 일부의 토큰)의 df가 `vrow`와 같다.
- pending 수를 보고한다.

결과는 JSON이다. 하나라도 다르면 0이 아닌 코드로 끝난다. 서비스는 검증 결과와 무관하게 `ready`면 DB 색인을 쓴다. 검증은 운영 절차(12절)의 전환 판정이다.

### 5.5 rollback

- 검색만 되돌리기: `OPENCRAB_BM25_DB_INDEX=0`으로 서비스를 다시 시작한다. DB는 바뀌지 않는다.
- 색인 제거: 서비스를 멈추고 `opencrab packs bm25-index drop`을 실행한다. 트리거와 색인 객체를 한 트랜잭션에서 지운다. `doc_nodes`와 다른 표는 건드리지 않는다. 이 코드가 다시 열면 객체를 새로 만들고 `backfill`로 시작하므로, 이전 코드로 되돌릴 때 이 명령을 쓴다.
- 이전 코드로 되돌리고 drop을 하지 않으면 트리거가 남는다. 이전 코드는 색인 표를 읽지 않는다. pending은 키마다 한 행이므로 `doc_nodes` 행 수를 넘지 않는다. 다시 이 코드로 올리면 열기 점검과 pending 소진이 그 사이 변경을 반영한다.
- 원본 유실: 모든 단계가 색인 객체만 쓴다. `doc_nodes`, `doc_sources`, vector, 그래프는 읽기만 한다.

## 6. #550과의 관계

#550은 이 이슈에 흡수하지 않는다. 새 색인은 같은 토큰 열과 같은 길이 정규화를 쓰므로 #550의 결함(한글 n-gram이 문서 길이에 들어가 긴 한국어 정답이 짧은 문서에 밀림)을 그대로 옮긴다. 기본 설정의 `top-k-pressure-1`은 base와 같은 `partial_silent`로 남는다. 축소 상한 설정에서는 base에서 창 밖 경고가 이 순위 실패를 가렸다. 새 색인은 창이 없으므로 경고가 사라지고 같은 질의가 `partial_silent`로 드러난다. 이 변화는 순위가 바뀐 결과가 아니라 경고가 사라진 결과다. 대조 보고(13절)에 질의별로 적는다. #550을 고치면 같은 공용 점수 함수와 토큰화 한 곳만 바꾸면 되고, 색인 형식 버전을 올려 backfill로 재색인한다.

## 7. 캐시

이 변경은 메모리 캐시를 두지 않는다. 이슈는 캐시를 허용하되 요구하지 않는다. DB 색인 질의는 cold 구축이 없고, 착수 전 probe에서 S1 질의 세트의 BM25 계산이 기존 메모리 색인보다 훨씬 빨랐다(날짜 기록). 캐시가 없으므로 메모리 상한, eviction, 단일 구축, 세대, 재시작 뒤 수렴을 정할 대상이 없다. 기존 메모리 창 경로의 상태 객체는 `backfill`, `unsupported`, 킬 스위치 상태에서만 만들어진다. S2 이상 측정에서 필요가 확인되면, stats 갱신마다 오르는 세대로 무효화하는 용어별 df 캐시를 후속으로 둔다. 그 캐시도 miss는 DB 질의로 이어진다.

## 8. 호출자와 형제 경로

- BM25 레그 진입점: `HybridQuery._bm25_search_state`, `_bm25_search_per_pack`, `_bm25_search_with_warnings`, `_bm25_search`. 앞의 둘이 DB 경로 분기를 갖고 뒤의 둘은 그 둘을 거친다. `pack_registry._choose_by_content`는 `_bm25_search_per_pack`을 부르고, 없으면 `_bm25_search`로 돌아간다.
- 무효화 호출자: `mcp/tools/graph.py`(두 곳), `mcp/tools/pack.py`, `HybridQuery.ingest`. 모두 `invalidate_bm25_cache()`를 부른다. DB 모드에서는 아무 일도 하지 않는다.
- `doc_nodes` 쓰기 지점: 저장소 메서드 셋(4.4절 첫 항목), `pack/load.py`의 팩 삭제 집합 DELETE, 운영 스크립트의 직접 SQL(`scripts/cleanup_openclaw_heartbeat_493.py`, `scripts/backfill_space.py` 등), #542 하네스의 `updated_at` 갱신. 앞의 것은 쓰기 동기화, 나머지는 트리거와 pending이 맡는다.
- 형제 저장소: `PgDocStore`와 `MongoStore`는 색인 능력을 노출하지 않는다. `HybridQuery`는 저장소의 `supports_node_bm25` 속성으로 분기하므로 두 저장소는 기존 경로를 그대로 쓴다.
- 결과 결합과 동일 본문 접기(#482), 정책 필터(`_policy_filter`), source 결과 결합은 BM25 결과 항목의 모양을 읽는다. 항목 모양이 같으므로 바뀌지 않는다.
- `opencrab packs reindex-bm25`는 CLI 프로세스 안에서 버려지는 메모리 색인을 만든다. 이 변경은 손대지 않고 후속 정리 후보로 둔다.

## 9. 시험 전략

새 시험은 `tests/test_node_bm25_index.py` 하나에 둔다. 모두 `tmp_path`의 실제 `LocalSQLDocStore`를 쓴다.

- 동등성(오라클): 여러 팩, 두 space, 한국어와 영어, 같은 본문 다른 ID, `max_text_chars`보다 긴 본문, 깨진 properties, 숫자 `pack_id`, 무소속 행을 담은 무작위 코퍼스를 만든다. 질의, 팩 집합, `spaces`, `limit`의 조합마다 DB 색인 결과가 `BM25Index.build(전량).search` 결과와 항목, 점수, 순서까지 같다. 팩별 탐침도 같은 방식으로 대조한다.
- 토큰 왕복: `tokenize` 결과를 FTS5에 넣고 `vinst`로 되읽은 용어 열이 원래와 같다(`_`, 숫자, 한글, 여러 문자 체계, 1024바이트를 넘는 긴 토큰의 대리 용어). 긴 토큰 하나로 묻는 질의가 그 토큰을 가진 문서를 찾고, 앞부분만 같은 다른 긴 토큰의 문서는 찾지 않는다.
- 트리거: upsert의 `DO UPDATE`, `INSERT OR REPLACE`, 키를 바꾸는 UPDATE, DELETE가 pending에 맞는 키를 남기고 실패하지 않는다.
- 창 무관성: `OPENCRAB_BM25_NODE_LIMIT`과 `OPENCRAB_BM25_CHAR_BUDGET`을 아주 작게 둔 프로세스 환경에서도 오래된 문서와 다른 팩 문서가 찾히고 coverage 경고가 없다.
- 쓰기 동기화: 삽입, 수정, 삭제, 팩 이동을 저장소 메서드로 한 직후 결과가 맞다. 직접 SQL 쓰기(다른 연결)는 pending에 남고 다음 검색이 소진한다. 소진 예산보다 많으면 pending 경고가 나오고, 그동안에도 지워진 노드와 팩 밖으로 옮긴 노드는 결과에 없다(적대 대조군).
- 권한과 top-k 결합: 요청 팩 밖의 고득점 문서가 많아도 결과가 요청 팩 문서로 채워진다. `spaces` 필터도 같다.
- 실패와 복구: 색인 DDL 실패를 주입하면 색인 객체가 하나도 남지 않고 저장소는 쓰기와 읽기가 된다. `reindex_key` 실패를 주입하면 문서 쓰기는 성공하고 pending이 남는다. backfill 단계 도중 예외를 주입하면 커서와 색인 행이 단계 전 상태다. 다시 실행하면 `ready`가 되고 오라클과 같다.
- 상태 전이: 기존 행이 있는 DB를 처음 열면 `backfill`이고 검색은 기존 경로다. `max_text_chars` 변경과 행 수 불일치가 `backfill`로 되돌린다. 킬 스위치와 `drop`이 기존 경로로 돌린다.
- 다중 연결: 같은 파일을 연 두 저장소 인스턴스에서 한쪽 쓰기가 다른 쪽 검색에 바로 나온다.
- CLI: `status`, `backfill`, `verify`, `drop`의 정상 경로 하나씩과 `verify`가 불일치를 찾는 경우.

기존 시험 가운데 메모리 색인의 내부(cold 구축, 지문, 창, 재구축 스레드)를 실제 `LocalSQLDocStore`로 고정한 모듈은 킬 스위치로 기존 경로를 고정한다. 기존 경로는 PG, MongoDB, `backfill` 상태에서 계속 쓰이므로 그 시험의 대상이 그대로 살아 있다.

SQLite가 3.43보다 낮아 색인을 만들 수 없는 환경에서는 DB 색인 시험을 명시 사유로 건너뛴다. 3.43 이상인데 색인이 `unsupported`이면 실패로 둔다.

## 10. 비로컬 저장소

| 저장소 | 지원 상태 | 동작 |
|---|---|---|
| `LocalSQLDocStore` (local, kuzu) | 지원 | DB 색인 |
| `PgDocStore` (pg) | 이 이슈에서 미지원 | 기존 메모리 창과 coverage 경고. 조용한 부분 결과가 아니다. |
| `MongoStore` (docker) | 미지원 | 같음 |

PG 지원은 같은 상태 기계와 트리거 pending 구조를 PL/pgSQL 트리거와 GIN 또는 별도 역색인으로 옮기는 후속 이슈 후보다. 검증에는 일회용 PG 컨테이너가 필요하다.

## 11. 하네스(#542) 변경

head의 하네스는 다음 세 가지만 바꾼다. 판정 함수, 질의 세트, 요구치 키 집합은 바꾸지 않는다.

- `load`: `updated_at` 일괄 갱신 뒤 저장소가 색인을 가지면 pending을 끝까지 소진한다. 갱신이 트리거로 모든 행을 pending에 넣기 때문이다. 운영에서 대량 직접 SQL 뒤 운영자가 하는 일과 같다.
- 자원: 색인 바이트에 `doc_nodes_bm25`로 시작하는 표를 더한다. 원문 본문 바이트에 `doc_nodes` 행의 검색 본문(`_node_text`의 UTF-8 바이트)을 더한다. 4.5절은 노드 색인을 색인에 넣으라고 정했고, 노드 색인의 원문은 노드 검색 본문이다. 보고서에는 구성 값을 따로 남긴다.
- 환경: 보고서 `environment`에 BM25 경로(`db` 또는 `memory`)를 남긴다.

DB 모드에서는 하네스의 세대 확인(`hq._bm25.state.generation`)이 늘 `None`이다. 품질 판정 구간에는 쓰기가 없으므로 판정이 달라지지 않는다.

## 12. 운영 절차 (라이브 적용, 이 이슈에서 실행하지 않음)

1. 승인: 리드와 사용자가 적용 시점과 이 절차를 승인한다.
2. 백업: 서비스를 멈추지 않고 기존 백업 도구로 `doc_store.db`를 포함한 백업 세트를 만들고 검증한다.
3. 배포: 새 코드로 서비스를 다시 시작한다. 열기가 색인 객체와 트리거를 만든다. 상태는 `backfill`이고 검색은 기존 경로다. 이때부터 새 쓰기는 색인된다.
4. backfill: 자원 상한(transient systemd 서비스, `CPUQuota`, `LimitAS`)을 걸고 `opencrab packs bm25-index backfill`을 실행한다. 중단되면 같은 명령으로 재개한다.
5. 검증: `opencrab packs bm25-index verify`가 일치를 보고하는지 확인한다. 상태가 `ready`가 되면 다음 검색부터 DB 색인을 쓴다.
6. 확인: 알려진 질의 몇 개로 결과와 경고를 확인한다.
7. rollback: 5.5절.

## 13. 대조 계획 (구현 뒤)

#542 하네스로 같은 seed(542), 같은 프리셋(S1), 두 설정(기본, 축소 상한)을 base(최신 `origin/main`)와 head에서 순차로 돌리고 `compare`로 대조한다. 기대는 다음과 같다.

- 기본 설정: 분류가 base와 같다. 점수 식과 범위가 같기 때문이다. `top-k-pressure-1`의 `partial_silent`는 #550이며 base에도 있다.
- 축소 상한 설정: 창 밖 누락으로 생긴 BM25와 결합의 `partial_flagged`가 사라지고 자동 선택 오류가 사라진다. `top-k-pressure-1`은 6절대로 `partial_silent`로 드러난다.
- 두 설정 모두 누출, 중복, 범위 오류, 경고 누락이 0이다.
- cold 지연과 peak RSS는 줄고, 쓰기 지연과 적재 시간은 늘 수 있다. 수치는 날짜 기록에 남긴다.

## 14. 이슈 작업 항목 대조

| 이슈 항목 | 처리 |
|---|---|
| FTS5 확장과 다른 저장소 비교, 새 의존성 입증 | 3절. 새 의존성 없음. |
| 한국어 토큰화, 검색 필드, BM25 순위, pack/space/ReBAC 경계, source와 node 결합 계약 | 4.2, 4.3, 4.5, 8절. 모두 기존과 같다. |
| 문자열 아닌 `pack_id`, 깨진 JSON, DDL 실패의 무변경 실패와 복구 | 4.2, 5.2, 5.3절. |
| 삽입, 수정, 삭제, 팩 삭제, 승격, 외부 쓰기의 동기화와 감지, #44 대조 | 4.4절. |
| 캐시의 상한, eviction, 단일 구축, 세대, 수렴 | 7절. 캐시를 두지 않는다. |
| 질의당 전역 COUNT/MAX와 전역 재구축의 대체 | 4.7절. |
| backfill, 재개, 전환 검증, rollback, 시작 때 장시간 구축 금지 | 5절, 12절. |
| 비로컬 저장소의 지원 상태, 조용한 부분 색인 금지 | 10절. |

## 15. 받아들임 기준 대조

| 기준 | 확인 수단 |
|---|---|
| 여러 대형 팩과 오래된 문서에서 행 상한이나 eviction 때문에 노드가 사라지지 않는다 | 창 무관성 시험, 하네스 축소 상한 대조. eviction 대상인 캐시는 없다. |
| 팩 지정, 자동 선택, 다중 팩, 정상과 오류 입력이 정답 집합과 일치, 순위 차이에 근거 | 오라클 동등성 시험, 하네스 두 설정 대조, 6절. |
| 권한 밖과 다른 팩 결과 비노출, 필터와 top-k 결합 적대 대조 | 권한과 top-k 시험, pending 중 팩 이동 시험, 하네스 누출 0. |
| 수정과 삭제 뒤 stale 결과 없음, 외부 쓰기 감지 경계 명시 | 쓰기 동기화 시험, 4.4절. |
| 마이그레이션 중단, 재시작, 실패, rollback에서 원본 유실과 승인되지 않은 부분 전환 없음 | 실패와 복구 시험, 상태 전이 시험, 5절. `ready` 전에는 DB 색인을 쓰지 않는다. |
| 검색 품질, cold/warm 지연, peak RSS, DB 크기, 쓰기 비용 측정 | 하네스 대조와 날짜 기록. 대형 규모(S2 이상)는 승인 뒤. |
| 게이트 레시피, 외부 설계와 구현 검증, 독립 실행 검증 | 전달 파이프라인. |

## 16. SOLID 판정

- 단일 책임: 색인 쓰기(`reindex_key`, 소진, backfill), 색인 읽기(검색), 운영(상태, 검증, 제거)을 한 모듈 `opencrab/stores/_node_bm25_sqlite.py`의 서로 다른 함수로 둔다. 호출자 집단은 저장소 쓰기 메서드, `HybridQuery`, CLI 셋이고 각자 쓰는 함수가 겹치지 않는다. 점수 식은 메모리 색인과 DB 색인이 함께 쓰므로 `bm25.py`의 함수 하나로 둔다.
- 개방 폐쇄: 저장소별 분기는 `supports_node_bm25` 속성 하나다. 새 저장소는 같은 메서드를 구현하면 된다.
- 리스코프 치환: DB 경로 결과 항목의 키, 값, 순서, 빈 범위 처리, `include_unpackaged` 무시는 기존 `BM25Index.search` 계약과 같다. 오라클 시험이 이를 고정한다. 경고는 기존과 같은 접두의 새 문자열만 더한다.
- 인터페이스 분리: 저장소의 새 메서드는 검색에 필요한 값(질의, 팩, space, limit)만 받는다.
- 의존 역전: `HybridQuery`는 문서 저장소 객체의 능력 속성과 메서드에만 기댄다. 구현은 SQLite 하나뿐이므로 별도 인터페이스 타입을 만들지 않는다.

## 17. 범위 밖

라이브 DB 변경, 라이브 서비스 전환, 라이브 backfill, S2 이상 측정, #550의 순위 수정, PG와 MongoDB 색인, `packs reindex-bm25` 정리, #534(기존 재구축 스레드 수명)의 수정은 하지 않는다. DB 모드에서는 재구축 스레드가 시작되지 않지만 기존 경로에는 #534가 그대로 남는다.

## 18. 실행 가이드

```bash
opencrab packs bm25-index status     # 상태, pending 수, 문서 수, 커서
opencrab packs bm25-index backfill   # 재개 가능한 backfill, 끝나면 ready
opencrab packs bm25-index verify     # 읽기 전용 정합 확인, 불일치면 0이 아닌 코드
opencrab packs bm25-index drop       # 트리거와 색인 객체 제거(서비스 정지 뒤)
```

`OPENCRAB_BM25_DB_INDEX=0`은 검색 경로만 기존 메모리 창으로 돌린다.
