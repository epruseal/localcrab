# #542 검색 요구치와 소형 기준선

이 문서는 #542의 첫 단계를 정한다. 첫 단계는 #541(노드 BM25의 DB 전량 색인 전환)을 설계하기 전에 검색 품질과 비용의 요구치를 고정하고, 같은 입력을 전환 전후에 다시 돌릴 수 있는 합성 코퍼스와 측정 하네스를 두는 일이다. 문서 앞부분은 설계이고 마지막 절은 실행 가이드다.

살아 있는 문서이므로 관측 수치를 적지 않는다. 관측 수치는 날짜를 박은 검증 기록(`docs/records/`)과 PR 본문에 둔다. 이 문서에 적힌 숫자는 요구치, 코퍼스 비율, 중단 조건처럼 측정 결과와 무관하게 설계 때 정한 값뿐이다.

## 1. 범위

이 단계가 하는 일은 다음과 같다.

- 목표 규모와 이 장치 등급(8GB RAM, 4코어 aarch64)에서 지켜야 할 품질, 지연, freshness, 자원 요구치를 정한다.
- 정답 집합과 보호 대조군(비정답, 권한 밖, 팩 밖, space 밖, 미완성 팩, 무소속 문서)을 질의별로 정의한다.
- BM25, FTS, vector, 결합 결과를 따로 판정하는 규칙을 정한다.
- 재현 가능한 합성 코퍼스 생성기, 정답 질의 세트, 측정 하네스를 저장소에 둔다.
- 소형 규모(S1)에서 기준선을 측정해 날짜를 박은 기록으로 남긴다.

이 단계가 하지 않는 일은 다음과 같다. 대형 규모(S2 이상) 측정, #541 구현, 성능 최적화, 라이브 서비스와 라이브 데이터 대상의 측정, 장애 주입 시험(타임아웃, 디스크 부족, 색인 오류, 중단 후 재개)은 이 단계에 넣지 않는다. 장애 주입은 #541이 새 색인과 backfill을 만든 뒤 그 경로를 대상으로 설계한다(10절). 이 단계는 기존 제품 코드를 바꾸지 않는다. 기존 파일 가운데 바꾸는 것은 쓰기 지점 목록 시험(`tests/test_write_sink_inventory.py`)에 더하는 선언 두 항목뿐이다. 그 시험은 store 쓰기 메서드를 직접 부르는 함수마다 사유 선언을 요구한다.

## 2. 현재 검색 경로 (소스 근거)

측정 대상 표면은 MCP `ontology_query`가 부르는 두 함수, `opencrab.services.pack_selection.resolve_packs`와 `opencrab.ontology.query.HybridQuery.query`다. MCP 전송, 과금 기록, canonical 보강, 응답 크기 처리(#511)는 이 두 함수 바깥에 있으므로 측정에서 뺀다. `ontology_query`는 `subject_id`를 넘기지 않으므로 `HybridQuery.query`의 정책 필터(`_policy_filter`)는 운영 MCP 경로에서 돌지 않는다. 하네스도 `subject_id`를 넘기지 않는다.

`HybridQuery.query`는 다음 레그의 결과를 합친다.

- vector: `_vector_search`가 vector store의 `query`를 `pack_id`와 `space` 조건으로 부른다. 요청 크기는 `_profile_for_query`의 `vector_limit`이다. 결과는 KNN이므로 점수가 낮아도 걸러지지 않는다. 이 하네스는 KURE 임베딩 서버를 쓰지 않으므로 vector 레그의 의미 품질은 판정하지 않는다(5.6절).
- BM25: `_bm25_search_with_warnings`가 프로세스 안 `BM25Index` 상태를 한 번 잡아 검색하고 같은 상태로 coverage 경고를 만든다. `HybridQuery.query`는 그 경고를 `QueryOutcome.warnings`에 더한다. 색인은 `doc_nodes`를 `updated_at DESC, space, node_id` 순으로 읽은 창이다. 창의 행 상한은 모듈 import 때 읽는 `OPENCRAB_BM25_NODE_LIMIT`이고, 문자 예산은 구축 때마다 읽는 `OPENCRAB_BM25_CHAR_BUDGET`이다(`list_nodes_within_budget`가 저장된 properties JSON 길이를 누적한다). 노드마다 `OPENCRAB_BM25_MAX_NODE_CHARS`까지만 토큰화한다. 창이 전량을 담지 못하면 `BM25 coverage partial rows` 경고를 낸다. 요청 팩이 창에 없다는 경고는 창이 전량을 담지 못했을 때만 낸다(#398). 요청 크기는 `bm25_limit`이다. 토큰화는 `text_cues.tokenize`이며 3글자 이상 한글 토큰에 2글자와 3글자 n-gram을 더한다.
- FTS: `_fts_search`가 `doc_sources_fts`(FTS5, `unicode61` 토크나이저)를 검색한다. 대상은 `doc_nodes`가 아니라 `doc_sources`의 본문이다. 기본 적재 경로(`text_as_node=True`)는 `doc_sources`를 쓰지 않으므로 FTS는 source로 적재한 문서만 찾는다. `unicode61`은 n-gram을 만들지 않으므로 조사가 붙은 한글 어절은 어간 질의와 맞지 않는다. FTS 결과의 `node_id`는 `metadata.node_id`가 없으면 `source_id`다. 요청 크기는 `bm25_limit`이다.
- graph: 앵커 노드에서 이웃을 넓힌다. #541과 무관하므로 이 단계는 graph store를 `available=False`로 두어 이 레그를 끈다.

결합 결과는 레그 결과의 rerank, 동일 본문 접기(#482), 상위 `limit` 자르기의 결과다.

권한 경계는 `read_scope`(주체가 소유하거나 `private`가 아닌 `ready` 팩)와 `in_pack_scope`(엄격한 `pack_id` 일치)로 정해진다. `resolve_packs`는 명시 `pack_ids`를 `narrow`로 읽기 범위와 교차한다. 하나라도 빠지면 `pack_ids_out_of_scope` 경고를 낸다. 교차 결과가 비면 `HybridQuery.query`는 store를 부르지 않고 빈 결과를 낸다. 팩을 명시하지 않으면 읽기 범위 전체가 대상이다. `auto_pack`이면 `choose_packs`가 SQL 팩 행과 manifest의 제목, 설명, 키워드, 태그, source label로 점수를 매긴다. 임계값을 넘는 후보가 없을 때만 `_choose_by_content`가 팩별 BM25와 FTS probe로 한 팩을 고른다.

## 3. 목표 규모와 단계

목표 규모는 이슈가 가정한 "여러 팩이 각각 5만 노드 이상"인 운영이다. 단계는 아래 비율로 정의하고 생성기 프리셋에 그대로 넣는다. 실제 팩의 수나 크기를 옮긴 값이 아니다.

| 단계 | 전체 노드(대략) | 대형 팩 | 소형 팩 | 다른 소유자 팩 | 이 단계에서 |
|---|---|---|---|---|---|
| S0 | 1천 | 2 | 3 | 비공개 1, 공개 1 | 저장소 시험(smoke) |
| S1 | 2만 | 2 | 3 | 비공개 1, 공개 1 | 소형 기준선 측정 |
| S2 | 6만 | 2(하나가 기본 행 상한을 넘음) | 3 | 비공개 1, 공개 1 | #541 설계 뒤 별도 승인 |
| S3 | 12만 | 3 | 4 | 비공개 1, 공개 1 | 별도 승인 |
| S4 | 25만 | 4(각 5만 이상) | 6 | 비공개 1, 공개 1 | 목표 규모, 별도 승인 |

모든 단계에 미완성(`creating`) 팩 하나, 무소속(빈 `pack_id`) 문서, 측정 전용 팩 하나가 들어간다. 측정 전용 팩은 freshness와 동시 쓰기에만 쓰며 질의 세트의 정답과 대조군을 갖지 않는다.

### 3.1 BM25 설정

하네스는 BM25 설정 세 값을 환경 변수로 명시해 넣고 보고서에 남긴다. 실행 셸의 기존 값에 기대지 않는다. S1은 두 설정으로 돈다.

- 기본 설정: 세 값을 코드 기본값과 같게 둔다(행 상한 50000, 문자 예산 200000000, 노드당 토큰화 글자 65536). 하네스는 import한 모듈의 기본 상수와 넣은 값이 같은지 대조하고, 다르면 보고서에 그 차이를 적는다.
- 축소 상한 설정: 행 상한만 전체 노드 수의 25%(올림)로 둔다. S2 이상에서 생길 창 밖 누락을 소형 규모에서 재현한다.

생성기와 하네스는 다음 불변식을 확인한다. 하나라도 어기면 `run`은 측정을 시작하지 않고 `INCOMPLETE`로 끝난다.

- 기본 설정에서 모든 `doc_nodes` 행의 저장 properties 길이 합이 문자 예산보다 작다. 그래서 기본 설정의 창은 전량을 담는다.
- "오래됨" 문서는 전역으로 가장 오래된 30%다. 생성기는 모든 팩의 오래된 문서에 다른 모든 문서보다 이른 `updated_at`을 준다. 축소 상한의 창(최근 25%)에 오래된 문서가 하나도 없다.
- "최근" 문서는 전역으로 가장 최근 10%다. 축소 상한의 창에 최근 문서가 전부 있다.
- 하네스는 측정 전에 BM25 구축이 쓰는 창 읽기 API(`list_nodes_within_budget(limit, max_chars)`, 실패 시 `list_nodes(limit)`에 같은 문자 예산 규칙)로 위 두 포함 관계를 실제 store에서 다시 확인한다.

## 4. 요구치

요구치는 측정 전에 정했다. 코드나 기준선 결과에 맞춰 사후에 완화하지 않는다. 기준선이 요구치를 어기면 그 사실을 기록하고, 그 위반이 #541과 후속 이슈의 대상이 된다.

### 4.1 측정 조건

모든 요구치는 아래 조건에서 판정한다.

- transient systemd 서비스: `CPUQuota=100%`(코어 하나), `Nice=19`, `IOSchedulingClass=best-effort`, `IOSchedulingPriority=7`, `LimitAS=2G`, `MALLOC_ARENA_MAX=2`, `PYTHONDONTWRITEBYTECODE=1`.
- 임베딩은 결정적 해시 임베딩이다(5.6절). 운영의 임베딩 지연은 포함하지 않는다.
- graph store는 끈다.
- 지연은 프로세스 안에서 `time.perf_counter()`로 잰 벽시계 시간이다.

운영은 코어 넷을 다른 서비스와 나눠 쓰고 `serve` 인스턴스를 둘 둔다. 코어 하나 조건은 운영보다 엄격하다. 인스턴스마다 색인을 따로 가지므로 자원 요구치는 프로세스 하나 기준으로 정하고 운영 총량은 인스턴스 수를 곱해 읽는다.

### 4.2 품질 요구치 (모든 단계, 위반 0)

- 누출 0: 레그가 돌려준 전체 목록, 결합 결과, 모든 접힌 구성원에 권한 밖, 요청 팩 밖, 요청 space 밖, 미완성 팩, 무소속 문서가 하나도 없다. 정답표에 없는 `node_id`도 누출로 센다. 판정은 결과 메타데이터가 아니라 생성기의 정답표로 한다.
- 중복 0: 결과 목록과 접힌 구성원을 펼친 목록에 같은 `node_id`가 두 번 나오지 않는다.
- 결합 누락 0: 결합 결과의 상위 `limit`(대표와 그 접힌 구성원)에 그 질의의 정답이 전부 있다. 어느 레그의 계약도 보장하지 않는 정답도 면제하지 않는다.
- 레그 누락 0: 각 레그는 자기 계약이 찾아야 하는 정답(6.3절의 must-hit)을 자기 목록의 상위 `limit` 안에 전부 낸다.
- 조용한 부분 결과 0: BM25 레그가 must-hit 정답을 놓쳤으면 그 레그 호출의 coverage 경고가 있어야 한다. 결합 결과가 정답을 놓쳤고 그 정답이 BM25 must-hit이면 결합 호출의 `warnings`에도 coverage 경고가 있어야 한다. BM25 레그 호출이 낸 coverage 경고는 같은 BM25 상태 세대의 결합 호출 `warnings`에도 있어야 한다.
- 범위 처리 정확성: 명시 `pack_ids`에 읽기 범위 밖 팩이 섞이면 `pack_ids_out_of_scope` 경고가 있고, 범위 밖 팩만 요청하면 결과가 비어 있다.
- 자동 선택 정확성: `auto_pack` 질의는 정답 팩을 고른다. 고른 팩은 읽기 범위 안에 있다.

### 4.3 지연 요구치 (warm, 모든 단계)

| 질의 형태 | p95 상한 |
|---|---|
| 명시 단일 팩, 결합 | 800ms |
| 명시 다중 팩 또는 전체 읽기 범위, 결합 | 2000ms |
| `auto_pack`(선택과 결합 질의 합계) | 2500ms |
| 레그 하나(BM25, FTS, vector 각각) | 500ms |
| 동시 쓰기 중 결합 질의 | 같은 형태 상한의 2배 |

cold 요구치: 프로세스 시작 뒤 첫 결합 질의는 15초 안에 답한다. 이 시간에는 BM25 첫 구축이 들어간다.

### 4.4 freshness 요구치

쓰기 호출이 돌아온 시점부터 잰다. 쓰기 경로는 운영 적재 경로처럼 store 쓰기 뒤 `invalidate_bm25_cache()`를 부른다. 각 판정 전에는 대상 문서가 그 레그에서 실제로 검색되는지 먼저 확인한다(삭제와 수정의 선행 조건).

| 변화 | BM25와 FTS의 판정 | vector의 판정 | 상한(BM25 / FTS / vector) |
|---|---|---|---|
| 새 문서 | 새 표지 토큰 질의의 상위 `limit`에 문서가 있다 | 문서 본문 질의의 상위 `limit`에 문서가 있다 | 10초 / 1초 / 1초 |
| 수정 | 새 표지 토큰으로 찾히고 옛 표지 토큰으로는 찾히지 않는다 | 새 본문 질의의 상위 `limit`에 문서가 있고 반환 본문이 새 본문이다 | 10초 / 1초 / 1초 |
| 삭제 | 표지 토큰 질의에 문서가 없다 | 본문 질의에 문서가 없다 | 10초 / 1초 / 1초 |

측정 전용 팩에는 채움 문서가 있으므로 vector KNN은 대상 문서 없이도 결과를 채운다.

### 4.5 자원 요구치 (프로세스 하나)

- peak RSS(`VmHWM`) 1.0GiB 이하. 인스턴스 둘이면 운영 총량은 2GiB다.
- 검색 색인이 차지하는 디스크는 원문 본문 바이트의 2배 이하. 검색 색인은 FTS shadow table(`doc_sources_fts`로 시작하는 테이블)과 #541 이후의 노드 색인이다. 바이트는 SQLite `dbstat` 가상 테이블의 `pgsize` 합으로 잰다. `dbstat`을 쓸 수 없으면 이 항목은 `unmeasured`이며 충족으로 치지 않는다.
- 측정 끝에 `PRAGMA wal_checkpoint(TRUNCATE)`가 `busy=0`으로 끝나고, 그 뒤 WAL 파일이 512MiB 이하다. `busy`가 0이 아니면 이 항목은 미충족이다.
- 동시 질의 중 노드 쓰기 하나의 지연 p95는 500ms 이하.
- 쓰기 잠금 대기 p95는 2초 이하. 잠금 대기는 쓰기마다 `opencrab.locking.write_lock(<실행 디렉터리>)`에 들어가기까지 걸린 시간이다. 이 단계의 쓰기 주체는 하나이므로 이 값은 경합 없는 기준선이다. 다른 쓰기 주체(백업, 두 번째 인스턴스)와의 경합은 S2 이후에 넣는다.

## 5. 합성 코퍼스

### 5.1 원칙

- 실제 팩 내용, 이름, 건수를 복사하지 않는다. 모든 문자열은 시드가 정한 생성 규칙으로 만든다.
- 같은 시드와 같은 프리셋은 바이트 단위로 같은 코퍼스를 낸다. 생성기는 산출물의 sha256을 manifest에 적는다.
- 생성기는 표준 라이브러리만 쓰고 문서를 스트리밍으로 쓴다. S4에서도 전체 본문을 메모리에 들지 않는다.

### 5.2 어휘

어휘는 세 종류다. 한글 의사 단어는 음절 표(`0xAC00`부터의 완성형 음절)에서 시드로 고른 2음절에서 4음절 조합이다. 영문 의사 단어는 자음과 모음 표의 조합이다. 정답 표지 토큰은 질의마다 하나씩 만든 희귀 토큰이다. 생성기는 표지 토큰과 그 n-gram이 일반 어휘와 겹치지 않음을 확인한다. 일반 어휘의 빈도는 Zipf 분포를 따른다.

### 5.3 주체와 팩

`opencrab.auth.create_user`는 사용자 행과 그 사용자의 기본 팩(`ensure_default_pack`)을 한 트랜잭션에서 만들지만 `user_id`를 무작위로 정한다. 무작위 ID는 `owner_id` 각인을 거쳐 노드 properties와 검색 본문에 들어가므로 적재마다 행이 달라진다. 하네스는 같은 계약을 고정 ID(`user_a`, `user_b`)로 재현한다. 한 트랜잭션에서 `users` 행을 넣고 같은 연결로 `ensure_default_pack(sql, user_id, conn=conn)`을 부른다. 기본 팩은 주체 A의 읽기 범위에 들어가는 빈 팩이며 기대 상태(8.2절)에 포함한다. 나머지 팩 행은 `opencrab.pack.ownership`의 API로 만든다.

| 역할 | 소유자 | 생성 API와 상태 | 공개 | 주체 A의 읽기 범위 |
|---|---|---|---|---|
| 대형 팩 | A | `create_pack`, `ready` | private | 안 |
| 소형 팩 | A | `create_pack`, `ready` | private | 안 |
| 측정 전용 팩 | A | `create_pack`, `ready` | private | 안 |
| 다른 소유자 비공개 팩 | B | `create_pack`, `ready` | private | 밖(권한 밖) |
| 다른 소유자 공개 팩 | B | `create_pack`, `ready`, 그 뒤 `set_visibility` | `public-read` | 안 |
| 미완성 팩 | A | `begin_pack_creation`, `creating` | private | 밖 |
| 무소속 문서 | 없음 | 팩 행 없음, 빈 `pack_id` | 없음 | 밖 |

`create_pack`이 접미사를 붙여 다른 ID를 돌려주면 하네스는 적재를 멈춘다. 팩 제목과 설명은 일반 어휘로만 만든다. 그래서 표지 토큰으로 묻는 `auto_pack` 질의는 `choose_packs`의 임계값을 넘지 못하고 콘텐츠 probe를 거친다. 제목 토큰으로 묻는 `auto_pack` 질의도 따로 둔다.

### 5.4 저장소별 행 계약

하네스는 운영 쓰기 경로가 store에 남기는 행을 store API로 직접 쓴다. graph store와 쓰기 게이트의 권한 확인은 거치지 않는다. 각인은 운영 경로와 같은 함수 `opencrab.pack.write_gate.stamp`를 그 문서의 소유자 주체로 불러 만든다. 검색 본문은 `opencrab.ontology.bm25._node_text`로 만든다. 행을 만드는 일은 store와 무관한 함수 하나(행 계약 함수)가 맡고, 적재와 기대 상태 계산(8.2절)이 같은 함수를 쓴다. 문서는 두 적재 형태 가운데 하나다.

| store | 노드 형태(N): `OntologyBuilder.add_node` | source 형태(S): `source_writer.write_source` |
|---|---|---|
| `doc_nodes` | `upsert_node_doc(space, node_type, node_id, P)`. `P = stamp(코퍼스 properties, keys=NODE_STAMPED)`이며 `pack_id`와 `owner_id`가 들어간다 | `upsert_node_doc("evidence", "TextUnit", source_id, P)`. `P = stamp({"pack_id", "text"}와 메타데이터에 있으면 "title", keys=NODE_STAMPED)` |
| `doc_sources` | 없음 | `upsert_source(source_id, text, M)`. `M = stamp(코퍼스 메타데이터, keys=SOURCE_STAMPED)`에 `space` 기본값 `evidence`를 채운 값이며 `pack_id`, `user_id`, `space`, 있으면 `title`이 들어간다 |
| vector | `upsert_texts([_node_text({node_id, node_type, P})], ids=[node_id], metadatas=[{"pack_id", "source": pack_id, "node_id", "space"}])`. 본문이 공백뿐이면 쓰지 않는다 | builder의 vector 쓰기는 없다(`write_vector=False`). `HybridQuery.ingest`와 같이 `upsert_texts([text], ids=[source_id], metadatas=[M와 "source_id"])`를 한 번 쓴다 |

source 형태의 `node_id`는 `source_id`와 같다. `doc_sources`의 메타데이터에는 `source_id`가 없다(운영 경로는 doc 행을 쓴 뒤에 같은 dict에 `source_id`를 넣는다). 무소속 문서는 각인을 거치지 않고 빈 `pack_id`로 쓴다. 미완성 팩과 권한 밖 팩의 문서는 그 팩 소유자의 각인으로 쓴다. 쓰기 뒤 하네스는 격리 DB의 `doc_nodes.updated_at`을 생성기 값으로 바꾼다. 행 계약이 운영 경로의 행과 같은지는 S0 시험이 두 형태 모두 실제 쓰기 함수와 대조해 확인한다(11절).

### 5.5 문서 특성

- 최근과 오래됨: 3.1절의 전역 비율을 따른다.
- 같은 본문, 다른 ID: 같은 팩 안의 쌍과 서로 다른 팩 사이의 쌍을 둔다.
- 긴 본문: 일부 문서는 노드당 토큰화 글자 기본값보다 길다. 정답 토큰을 그 경계 뒤에 둔 문서가 있고, 그 가운데 source 형태와 노드 형태가 모두 있다.
- 한국어 표기 변형: 정답 문서에는 어간에 조사가 붙은 어절이나 띄어쓰기를 바꾼 형태만 있고, 질의는 어간만 쓴다.
- source 형태: 문서의 25%다. 나머지는 노드 형태다.
- space: 노드 형태 문서의 대부분은 `evidence`이고 일부는 `concept`이다. source 형태는 `evidence`다.

### 5.6 vector와 임베딩

임베딩은 `tokenize` 결과의 각 토큰을 해시해 고정 차원(64) 벡터에 더하고 정규화한 결정적 함수다. 네트워크와 모델 파일을 쓰지 않는다. 이 함수는 의미를 모델링하지 않으므로 vector 레그의 재현율은 판정하지 않는다. vector 레그는 누출과 중복을 판정하고 지연과 freshness를 잰다.

## 6. 질의 세트, 정답, 대조군

### 6.1 질의 종류

생성기는 아래 종류마다 여러 질의를 만든다. 각 질의는 `qid`, 종류, 질의문, 요청 `pack_ids` 또는 `auto_pack`, `spaces`, `limit`, 정답 집합, 비정답 대조군, 금지 집합, 레그별 must-hit 집합, 기대 경고, 기대 선택 팩을 가진다. 정답 집합의 크기는 `limit`의 절반 이하다.

| 종류 | 내용 | 정답 | 금지 집합 |
|---|---|---|---|
| exact-recent | 최근 문서의 표지 토큰, 명시 단일 팩 | 그 문서 | 다른 팩의 같은 토큰 문서 |
| exact-old | 오래된 문서의 표지 토큰, 명시 단일 팩 | 그 문서 | 같음 |
| explicit-multi | 허용 팩 둘에 나뉜 표지 토큰, 두 팩 명시 | 두 팩의 문서 | 명시하지 않은 팩의 같은 토큰 문서 |
| explicit-mixed | 허용 팩 하나와 권한 밖 팩과 미완성 팩을 함께 명시 | 허용 팩 문서, `pack_ids_out_of_scope` 경고 | 권한 밖과 미완성 팩의 같은 토큰 문서 |
| explicit-forbidden | 권한 밖 팩만 명시 | 없음(빈 결과), `pack_ids_out_of_scope` 경고 | 권한 밖 팩의 같은 토큰 문서 |
| whole-scope | 대형 팩과 공개 팩에 나뉜 표지 토큰, 팩 미지정 | 두 팩의 문서(공개 팩 정답 포함) | 권한 밖, 미완성, 무소속의 같은 토큰 문서 |
| auto-content | 소형 팩 문서의 표지 토큰, `auto_pack` | 그 문서, 그 팩 선택 | 다른 팩의 같은 토큰 문서 |
| auto-title | 팩 제목 토큰, `auto_pack` | 그 팩 선택 | 없음(선택만 판정) |
| scope-filter | 대형 팩 둘에 같은 토큰, 하나만 명시 | 명시 팩 문서 | 다른 대형 팩 문서 |
| space-filter | 같은 토큰이 `concept`과 `evidence`에 있음, `spaces=[concept]` | `concept` 문서 | `evidence` 문서 |
| korean-variant | 조사가 붙은 어절만 있는 문서, 어간 질의 | 그 문서 | 다른 팩의 같은 어절 문서 |
| long-tail | 긴 본문의 경계 뒤 표지 토큰 | 그 문서 | 없음 |
| duplicate-body | 같은 팩 안 같은 본문 두 ID와 다른 팩 같은 본문, 한 팩 명시 | 명시 팩의 두 ID | 명시 팩 밖의 같은 본문 문서 |
| top-k-pressure | 희귀 토큰과 흔한 토큰을 함께 쓴 질의, 흔한 토큰만 많이 가진 비정답 다수 | 희귀 토큰 문서 | 다른 팩 문서 |

### 6.2 비정답 대조군

비정답 대조군은 읽기 범위 안에 있지만 정답이 아닌 문서다. 표지 토큰의 일부 글자만 공유하거나 흔한 토큰만 반복한다. 대조군이 정답보다 앞에 오면 `decoy_above_gold`로 센다. 이 수치는 순위 품질 지표이며 4.2절의 위반 항목이 아니다. 대조군이 있으므로 "아무 문서나 상위에 오면 통과"하는 판정이 나오지 않는다.

### 6.3 레그별 must-hit

생성기는 각 레그의 문서화된 계약에서 must-hit을 계산한다. 레그 계약 밖의 누락은 그 레그에서 `contract_miss`로 기록하고 레그 위반으로 세지 않는다. 결합 판정에는 이 면제가 없다.

- BM25 must-hit: `doc_nodes`에 있고, 표지 토큰이 `_node_text`의 앞 노드당 토큰화 글자 안에 있는 정답. 창의 행 상한과 문자 예산은 계약이 아니다. 창 밖이라서 놓치면 누락이다.
- FTS must-hit: source 형태이고, 표지 토큰이 `unicode61` 토큰과 정확히 같은 형태로 본문에 있는 정답.
- vector must-hit: 없음(5.6절).
- 결합 must-hit: 그 질의의 정답 전부. 긴 본문 경계 뒤의 노드 형태 정답처럼 어느 레그도 보장하지 않는 정답을 결합이 놓치면 결합 `miss`다. 이 결과는 현재 구현의 한계로 기준선에 기록된다.

## 7. 판정 규칙

### 7.1 호출과 판정 입력

하네스는 MCP 요청 하나를 "요청"이라 부른다. 요청은 `resolve_packs` 호출과 그 결과의 `effective_pack_ids`로 부른 `HybridQuery.query`다. 반복마다 요청을 새로 하므로 `auto_pack`의 콘텐츠 probe 비용이 매 표본에 들어간다. 요청 뒤에 같은 `resolve_packs` 결과로 레그 셋을 따로 부른다. 레그 호출의 요청 크기는 `HybridQuery.query`가 그 레그에 주는 크기(`_profile_for_query`)와 같다. 품질 판정 구간에는 쓰기가 없다. 하네스는 요청 앞과 레그 호출 뒤에 BM25 상태의 세대(`state.generation`)를 읽고, 같지 않으면 그 질의를 다시 돈다. 세 번 연속 다르면 그 질의는 `unstable`로 `INCOMPLETE` 사유가 된다.

판정 함수는 질의 묶음 하나만 받는 순수 함수다. 묶음은 질의의 정답표 항목, `resolve_packs`의 결과, 레그마다의 결과 목록과 경고, 결합 결과(접힌 구성원 포함)와 그 경고로 이루어진다. 결합의 `warning_dropped`는 같은 묶음 안의 BM25 레그 경고와 결합 경고를 비교해 정한다. 레그와 결합 결과 각각에 대해 다음을 계산한다.

- `leak`: 돌려준 전체 목록과 모든 접힌 구성원 가운데 금지 집합, 요청 범위(`effective_pack_ids`) 밖, 요청 space 밖, 읽기 범위 밖에 있는 문서와 정답표에 없는 `node_id`의 수. 레그는 상위 `limit`이 아니라 돌려준 전체 목록을 본다.
- `dup`: 결과 목록과 접힌 구성원을 펼친 목록에서 반복된 `node_id`의 수.
- `missing`: must-hit 가운데 상위 `limit`(결합은 대표와 구성원)에 없는 정답의 수.
- `contract_miss`: 레그에서 must-hit이 아닌 정답 가운데 상위 `limit`에 없는 수.
- `gold_rank`: 상위 `limit` 밖 정답의 전체 목록 안 순위(없으면 비움).
- `decoy_above_gold`: 정답보다 앞에 있는 비정답 대조군의 수.
- `coverage_warnings`: 그 호출이 낸 coverage 경고. 결합은 `QueryOutcome.warnings`에서 고른다.

### 7.2 질의 분류

한 질의의 한 레그(또는 결합)는 아래 순서에서 처음 맞는 분류를 받는다.

1. `leak`: `leak > 0`.
2. `dup`: `dup > 0`.
3. `scope_error`: 기대한 `pack_ids_out_of_scope` 경고가 없거나, 범위 밖 팩만 요청했는데 결과가 있다.
4. `warning_dropped`(결합만): BM25 레그 호출이 coverage 경고를 냈는데 같은 세대의 결합 호출 경고에 그 경고가 없다.
5. `partial_silent`: BM25 레그가 `missing > 0`이고 coverage 경고가 없다. 결합은 놓친 정답 가운데 BM25 must-hit이 있고 결합 경고에 coverage 경고가 없다.
6. `partial_flagged`: 5번 조건에서 coverage 경고가 있다.
7. `miss`: `missing > 0`.
8. `pass`.

`auto_pack` 질의는 선택한 팩이 기대 팩이 아니면 결합 분류 앞에 `wrong_pack`을 붙인다. 4.2절의 품질 요구치는 `pass`만 충족으로 본다. `partial_flagged`는 조용한 부분 결과 요구는 지키지만 누락 요구를 어긴다.

### 7.3 실행 판정

하네스 보고서의 첫 키 `verdict`는 실행 자체가 온전했는지를 적는다. `COMPLETE`는 다음을 모두 만족한다는 뜻이다.

- 적재 완료 표시가 있고, 코퍼스 digest와 저장소별 행 수와 내용 digest가 코퍼스와 일치한다(8.2절).
- 3.1절 불변식이 성립한다.
- 계획한 질의를 전부 판정했고 `unstable` 질의가 없다.
- 중단 조건에 걸리지 않았다.

그 아래에 요구치별 충족 여부를 적는다. 기준선이 요구치를 어긴 것은 실행 실패가 아니다.

### 7.4 전환 전후 대조 (#541 이후)

#541 구현 뒤에는 같은 시드, 같은 프리셋, 같은 설정으로 base와 변경 브랜치를 순차로 돌린다. 하네스의 `compare`가 두 보고서를 질의와 레그 단위로 대조한다. 회귀는 다음 셋이다.

- 어느 질의든 base의 `pass`가 변경 뒤 다른 분류로 바뀐다.
- 변경 뒤 `leak`, `dup`, `scope_error`, `warning_dropped`, `partial_silent`가 하나라도 생긴다.
- 변경 뒤 요구치를 어기는 지연, freshness, 자원 항목이 base보다 늘어난다.

둘째 회귀는 마지막 반복의 분류만이 아니라 head 보고서의 `summary.repeat_defects`(warm 모든 반복의 독립 결함 표지 수와 동시 쓰기 구간의 누출과 중복 수)도 본다. 어느 표지든 0보다 크면 base의 같은 값과 무관하게 회귀다.

대조 전제는 다섯이다. 두 보고서가 `COMPLETE`다. 코퍼스 manifest digest, 프리셋, 시드가 같다. 설정과 BM25 환경 값이 같다. 질의 ID 집합이 같고 질의마다 레그 이름 집합이 같다. 요구치 키 집합이 같다. 하나라도 어기면 `compare`는 회귀를 내지 않고 `INVALID`와 사유를 출력하고 종료 코드 2로 끝난다.

토큰화나 순위가 달라져 분류가 바뀌면 질의마다 원인을 적는다. 근거 없이 등가라고 주장하지 않는다.

## 8. 측정 하네스

### 8.1 구성

- `scripts/search_corpus.py`: 생성기. `generate --preset <S0|S1|...> --seed <n> --out <코퍼스 디렉터리>`가 `docs.jsonl`, `packs.json`, `queries.json`, `manifest.json`을 쓴다. 출력 디렉터리가 이미 있으면 거부한다.
- `scripts/search_baseline.py`: 하네스. 하위 명령은 넷이다.
  - `load --corpus <코퍼스 디렉터리> --work <적재 디렉터리>`: 적재 디렉터리를 새로 만들고 5.3절과 5.4절대로 적재한다. 마지막에 적재 완료 표시를 쓴다.
  - `run --corpus <코퍼스 디렉터리> --work <적재 디렉터리> --run-dir <실행 디렉터리> --out <보고서> [--config default|scaled-cap] [--repeats <n>]`: 적재 디렉터리를 실행 디렉터리로 복사한 뒤 복사본에서만 측정한다.
  - `compare <base 보고서> <head 보고서>`: 7.4절 대조.
  - `selftest`: 판정 함수에 누출, 접힌 구성원 누출, 정답표에 없는 ID, 중복, 누락, 조용한 부분 결과, 정답을 모두 찾았지만 결합 경고만 빠진 경우, 범위 오류를 하나씩 심은 묶음을 주고 각각 해당 분류가 나오는지 확인한다. 저장소와 파일을 쓰지 않는다.

판정 함수는 store 구성과 측정 코드와 분리한다.

### 8.2 적재 완료 표시와 초기 상태

기대 상태는 코퍼스에서 계산한다. 계산은 코퍼스 파일과 행 계약 함수(5.4절)만 쓰고 store를 읽지 않는다. 기대 상태는 아래 다섯 표의 정렬된 행 해시와 행 수다.

| 표 | 해시에 넣는 열 |
|---|---|
| `doc_nodes` | `space`, `node_id`, `node_type`, properties(정규 JSON), `updated_at` |
| `doc_sources` | `source_id`, `text`, metadata(정규 JSON) |
| vector | id, 저장 본문(`document`), metadata(정규 JSON) |
| `packs` | `pack_id`(기본 팩은 아래 정규화 값), `owner_id`, `status`, `visibility`, `title`, `description`, `is_default` |
| `users` | `user_id`, `display_name`, `is_local` |

`packs`와 `users`에는 기본 팩과 고정 ID 사용자가 들어간다. 시각 열(`created_at` 등)은 해시에 넣지 않는다.

`ensure_default_pack`은 새 기본 팩의 ID를 무작위(`secrets.token_hex`)로 정하므로 새 적재마다 기본 팩 ID가 다르다. 대사는 `is_default`가 참인 행의 `pack_id`를 `default:<owner_id>`로 바꿔 해시한다. 대신 다음 두 조건을 따로 확인한다. 기본 팩 ID는 모듈의 기본 팩 접두사(`_DEFAULT_PACK_ID_PREFIX`)로 시작한다. 기본 팩 ID를 `pack_id`로 가진 `doc_nodes`, `doc_sources`, vector 행이 하나도 없다. 코퍼스는 기본 팩에 문서를 두지 않으므로 이 정규화는 검색과 권한에 영향을 주는 값을 숨기지 않는다. 주체 A의 기본 팩은 읽기 범위에 들어가지만 비어 있다.

- `load`는 모든 쓰기를 마친 뒤 store를 닫고 관측 상태를 계산한다. 관측 상태가 기대 상태와 같을 때만 `load-complete.json`을 마지막으로 쓴다. 이 파일에는 코퍼스 manifest digest와 기대 상태가 있다. 같지 않으면 `load`는 차이 나는 표 이름을 출력하고 0이 아닌 코드로 끝난다.
- 관측 상태는 store 생성자가 아니라 읽기 전용 SQLite 연결(`file:<경로>?mode=ro` URI)로 읽는다. store 생성자는 `write.lock`, WAL 설정, DDL을 실행하므로 대사에 쓰지 않는다. vector의 본문과 메타데이터는 vec0 테이블의 보조 열을 같은 읽기 전용 연결로 읽는다(sqlite-vec 확장을 읽기 전용 연결에 적재한다).
- `run`은 시작할 때 이 파일이 없거나, 코퍼스 manifest digest와 코퍼스에서 다시 계산한 기대 상태가 파일의 값과 다르면 측정 없이 `INCOMPLETE`로 끝난다.
- `run`은 적재 디렉터리의 DB 파일(WAL과 SHM 포함)을 새 실행 디렉터리로 복사하고, 복사본의 관측 상태를 같은 읽기 전용 방식으로 계산해 기대 상태와 대사한다. 다르면 `INCOMPLETE`다. 실행 디렉터리가 이미 있으면 거부한다. 부분 적재, 잘못된 적재, 이전 실행의 잔여 쓰기는 이 대사에서 걸린다. 두 설정은 같은 초기 상태에서 시작하고, freshness와 동시 쓰기는 적재 디렉터리를 바꾸지 않는다.

### 8.3 측정 순서 (`run`)

1. 환경 기록: Python 버전, 플랫폼, `/proc/self/limits`의 주소 공간 상한, `/proc/self/cgroup`이 가리키는 cgroup의 `cpu.max`, 넣은 BM25 설정 세 값과 모듈 기본 상수.
2. 불변식 확인: 7.3절과 3.1절.
3. cold: 프로세스의 첫 BM25 접근은 질의 계획의 첫 질의(명시 단일 팩, `auto_pack` 아님)의 요청이다. 이 요청의 지연을 cold 지연으로 기록한다. 그 안의 BM25 첫 구축 시간은 하네스가 `_bm25.ensure_built`를 감싼 시간 측정으로 따로 기록한다. 이 요청보다 먼저 `resolve_packs`의 콘텐츠 probe나 레그 호출이 BM25를 건드리지 않는다.
4. warm: 질의마다 요청과 레그 호출을 한 번 데운 뒤 `repeats`번 돈다. 매 반복은 요청(선택부터 결합 반환까지)의 지연과 레그 호출 각각의 지연을 따로 기록한다. 질의 형태별 p50, p95, 최댓값을 낸다.
5. 품질 판정: 마지막 warm 반복의 결과로 7절 판정을 한다. 모든 반복의 `leak`과 `dup`도 센다.
6. freshness: 측정 전용 팩에서 4.4절대로 새 문서, 수정, 삭제를 잰다. 레그마다 100ms 간격으로 30초까지 확인한다. 30초 안에 바뀌지 않으면 `not_converged`로 기록한다. 쓰기와 삭제는 운영 적재 도구처럼 store 쓰기와 `invalidate_bm25_cache()`를 같은 `write_lock(<실행 디렉터리>)` 블록 안에서 한다.
7. 동시 쓰기: 쓰기 스레드가 측정 전용 팩에 초당 5건의 노드 형태 쓰기를 60초 동안 한다(모두 300건). 쓰기마다 `write_lock(<실행 디렉터리>)` 안에서 store 쓰기와 `invalidate_bm25_cache()`를 하고, 잠금 대기와 쓰기 지연을 따로 기록한다. 같은 60초 동안 주 스레드는 질의 세트를 끝날 때까지 반복한다. 쓰기 일정은 질의 속도와 무관하다. 이 구간의 질의 결과에도 누출과 중복 판정을 한다. 측정 전용 팩의 문서는 정답표에 등록된 ID이므로 누출 판정에서 정답표 밖 ID로 세지 않는다.
8. 자원: 감시 스레드가 200ms마다 `VmRSS`를 읽고, 끝에 `VmHWM`, 단계별 CPU 시간, DB와 WAL 파일 크기, `dbstat`의 테이블별 바이트, checkpoint 결과를 기록한다.

### 8.4 보고서

보고서는 JSON이다. 첫 키 `verdict`는 `COMPLETE`, `INCOMPLETE`(사유 포함), `ABORTED`(중단 조건 이름 포함) 가운데 하나다. 그 아래에 환경, 코퍼스 digest, 질의별 레그별 판정, 요약(분류별 수, 지연 분위수, freshness, 자원), 요구치별 충족 여부를 둔다.

## 9. 격리와 안전

### 9.1 경로

| 경로 | 읽기 | 쓰기 |
|---|---|---|
| 저장소 소스 | `run`, `load`가 import | 없음(`PYTHONDONTWRITEBYTECODE=1`) |
| 코퍼스 디렉터리 | `load`, `run` | `generate`만 |
| 적재 디렉터리 | `load`, `run`(복사 원본) | `load`만. `doc.db`, `vectors.db`, `sql.db`와 그 WAL, SHM, `write.lock`, `load-complete.json` |
| 실행 디렉터리 | `run` | `run`만. 복사한 DB와 그 WAL, SHM, `write.lock` |
| 보고서 경로 | 없음 | `run` |
| `TMPDIR` | 라이브러리 임시 파일 | 라이브러리 임시 파일 |

- 하네스는 위 경로를 모두 인자로 받고 store를 그 경로로 직접 만든다. SQL URL은 `sqlite:///<디렉터리>/sql.db`로 하네스가 만들며 `Settings`의 URL을 쓰지 않는다. `resolve_packs`의 `local_data_dir`는 실행 디렉터리이며 manifest가 없으므로 enrichment는 비어 있다.
- 하네스는 모든 쓰기 경로가 저장소 트리 밖이고 `Settings().local_data_dir`와 같지도 그 안도 아님을 확인한 뒤에만 시작한다. 쓰기 경로가 읽기 원본(코퍼스 디렉터리, 적재 디렉터리) 안이거나 그것을 포함해도 거부한다. `run`은 보고서 경로가 이미 있으면 거부한다.
- 모든 하위 명령은 환경 변수 `TMPDIR`이 설정되어 있고 위 검사를 통과할 때만 시작한다.
- smoke 규모가 아닌 실행은 `TMPDIR`이나 쓰기 경로가 RAM 위 파일 시스템(`tmpfs`, `ramfs`)에 있으면 거부한다. 파일 시스템 종류는 `/proc/self/mountinfo`에서 경로 요소 경계로 가장 길게 일치하는 마운트로 판정하고, 판정할 수 없으면 RAM 위로 본다. 경로 이름(`/tmp`)이 아니라 매체로 판정하므로 디스크 위 `/tmp`(일반 CI 러너)는 허용하고 다른 위치에 마운트한 tmpfs는 거부한다. 가드는 경로를 실제 경로(`Path.resolve()`와 같은 의미, 심볼릭 링크를 `..`보다 먼저 푼다)로 푼 뒤 매체를 판정한다.
- smoke 규모는 코퍼스의 실제 `docs.jsonl`이 4MiB 이하이고 2000줄 이하인 경우다(`generate`는 생성기 프리셋 표의 전체 노드 수가 2000 이하인 경우). manifest의 프리셋 문자열은 판정에 쓰지 않는다. smoke 규모는 저장소 시험이 쓰는 S0이며 RAM 위 경로를 허용한다. 이 허용을 켜고 끄는 환경 변수나 플래그는 없다.
- 하네스는 KURE 임베딩 함수, 원격 서버, MCP 엔드포인트, docker 자원을 쓰지 않는다.
- 모든 실행(`generate`, `load`, `run`, `selftest`)은 4.1절의 transient systemd 서비스로 묶는다. 이 커널은 memory cgroup을 끈 상태라 `MemoryMax`가 동작하지 않는다. 메모리 보호는 `LimitAS`와 아래 감시 조건이 맡는다.

### 9.2 즉시 중단 조건

`generate`, `load`, `run`은 같은 감시 스레드를 둔다. 감시 스레드가 아래 조건을 보면 보고서(`load`와 `generate`는 작업 디렉터리의 `aborted.json`)에 `ABORTED`와 조건 이름을 원자적으로 쓴 뒤 `os._exit(3)`으로 프로세스를 끝낸다. 주 스레드가 질의 안에서 반환하지 않아도 감시 스레드가 종료를 맡는다. `load`가 중단되면 적재 완료 표시가 없으므로 그 적재 디렉터리는 `run`에서 쓰이지 않는다.

- 프로세스 RSS가 1.6GiB를 넘는다(`LimitAS=2G` 앞의 여유).
- 장치의 `MemAvailable`이 1GiB 아래로 내려가거나 남은 swap이 1GiB 아래로 내려간다.
- store를 부르는 호출 하나가 60초를 넘는다(`run`). 주 스레드의 요청, 레그 호출, freshness 확인과 쓰기 스레드의 쓰기 하나를 각자의 감시 슬롯으로 잰다.
- 쓰기 경로 파일 시스템의 남은 공간이 10GiB 아래로 내려간다. smoke 규모(9.1절)는 512MiB를 문턱으로 쓴다.
- 시간 예산을 넘는다. `generate`와 `load`를 합친 예산은 S0 5분, S1 30분, S2 60분, S3 120분, S4 240분이다. `run`의 예산은 S0 5분, S1 30분, S2 60분, S3 90분, S4 180분이다. 생성기는 끝날 때 코퍼스 디렉터리에 경과 시간 파일(`generate-timing.json`, manifest의 digest 대상 밖)을 쓰고, `load`는 합계 예산에서 그 시간을 뺀 나머지를 예산으로 쓴다. 그 파일이 없는 코퍼스는 `load`가 거부한다.

## 10. 단계 확대 계획

- S1의 선행 조건은 둘이다. `selftest`가 통과했고, S0 종단 시험(11절)이 통과했다.
- 다음 단계로 넘어가는 조건은 셋이다. 앞 단계가 `COMPLETE`로 끝났다. 판정기 `selftest`가 통과했다. 앞 단계 peak RSS를 노드 수에 비례해 다음 단계로 늘린 추정이 1.6GiB 아래다.
- S2 이상은 #541 설계 뒤 리드와 사용자의 승인을 받고 한 단계씩 돈다. 한 번에 한 측정만 돌린다.
- 누출이나 `partial_silent`가 나오면 그 단계는 끝까지 돌려 기록하되 다음 단계로 넘어가지 않는다.
- #541 구현 뒤 같은 단계에서 7.4절 대조를 하고, 그때 장애 주입(타임아웃, 취소, 디스크 부족, 색인 오류, 중단 후 재개, 재시작 수렴)을 새 색인 경로 대상으로 추가한다. 장애 주입의 판정은 "명시 오류 또는 검증된 fallback, 부분 결과를 완전 결과로 보고하지 않음"이다.
- 라이브 검증은 이 계획 밖이며 별도 승인 뒤에만 한다.

## 11. 시험 전략

`tests/test_search_baseline.py`를 둔다. 시험은 `tmp_path`만 쓰고 저장소에 파일을 남기지 않는다.

- 생성기 결정성: 같은 시드의 S0 코퍼스 두 벌의 digest가 같고, 다른 시드는 다르다. 정답표 불변식(표지 토큰의 유일성, 금지 집합과 정답의 서로소, 3.1절 전역 비율)을 확인한다.
- 판정기 검출력: `selftest`와 같은 심은 입력으로 각 분류가 나오는지 확인한다.
- 행 계약: 노드 형태 문서 하나와 source 형태 문서 하나를 두 번 쓴다. 한 번은 하네스의 행 계약으로, 한 번은 실제 운영 함수로 쓴다. 운영 함수는 노드 형태가 `OntologyBuilder.add_node`, source 형태가 `source_writer.write_source`다. 둘 다 기존 시험과 같은 `LocalGraphStore`, 실제 SQL store, 해시 임베딩 vector store, 팩 소유 주체의 `principal_scope`로 부른다. 두 결과의 `doc_nodes` 행(`node_type`, properties), `doc_sources` 행(본문, metadata), vector 행(id, 본문, metadata)이 같은지 비교한다.
- S0 종단 실행: `load`와 `run`을 S0, `repeats=1`로 돌려 `verdict`가 `COMPLETE`이고 모든 질의가 판정되며 누출, 중복, `scope_error`, `warning_dropped`가 0이다. 적재 디렉터리를 복사한 사본에서 각각 노드 행 하나의 `node_type`, vector 행 하나의 본문, 팩 행 하나의 `visibility`를 바꾼 뒤 `run`이 `INCOMPLETE`로 끝나는지도 확인한다.

판정기 검출력 시험은 역변이로 확인한다. 판정 함수에서 누출 검사를 끄면 그 시험이 실패해야 한다(gate-recipes 6절 역변이 소절의 수동 절차).

### 11.1 SOLID 판정

새로 쓰는 코드는 생성기, 판정기, 측정 하네스 셋이다. 판정기는 정답표 항목, 결과, 경고, 범위 결과만 받는 함수로 두어 store와 측정 코드에 의존하지 않는다(단일 책임, 인터페이스 분리). 하네스는 경로를 인자로 받아 store를 만든다. 다른 구현을 끼울 호출자가 하네스 자신과 S0 시험뿐이므로 별도 인터페이스 타입은 만들지 않는다(의존 역전 항목의 "구현이 하나뿐" 조건). 기존 제품 코드는 바꾸지 않는다.

하네스는 `HybridQuery`의 비공개 레그 메서드(`_bm25_search_with_warnings`, `_fts_search`, `_vector_search`)와 BM25 상태(`_bm25.state`)를 직접 읽는다. 레그를 따로 판정할 공개 표면이 없기 때문이다. S0 종단 시험이 이 호출을 거치므로 시그니처가 바뀌면 시험이 실패한다.

## 12. 이 단계의 받아들임 기준

- 이 문서가 Codex 설계 검증에서 PASS를 받는다.
- `selftest`와 `tests/test_search_baseline.py`가 통과하고, 판정기 역변이에서 시험이 실패한다.
- 생성기가 같은 시드로 같은 digest를 낸다.
- 하네스가 transient systemd 서비스 안에서 S1을 두 설정(기본, 축소 상한)으로 돌려 `COMPLETE` 보고서를 낸다. 실행 중 `/proc/<pid>/limits`와 cgroup `cpu.max`를 보고서에 남긴다.
- 기준선 수치는 `docs/records/`의 날짜를 박은 기록에만 있다.
- 이 단계는 기존 코드의 동작을 바꾸지 않는다.

## 13. 범위 밖과 후속 연결

기준선에서 확인한 병목은 이 단계에서 고치지 않는다. 근거는 기존 이슈(#534, #331, #320, #413, #283, #44, #522, #526)에 덧붙일 후보로 리드에게 보고한다. 이슈가 없는 독립 결함은 새 이슈 후보로 보고한다.

## 14. 실행 가이드

명령은 저장소 루트에서 실행한다. `<워크트리>`는 대상 워크트리, `<스크래치>`는 저장소 밖 디스크 경로다. 설치와 import 경로 확인은 `docs/gate-recipes.md` 1절과 2절을 따른다.

공통 실행 틀은 아래와 같다. `<명령>` 자리에 아래 단계의 명령을 넣는다.

```bash
systemd-run --user --wait --collect \
  -p CPUQuota=100% -p Nice=19 -p IOSchedulingClass=best-effort \
  -p IOSchedulingPriority=7 -p LimitAS=2G \
  -E MALLOC_ARENA_MAX=2 -E PYTHONDONTWRITEBYTECODE=1 \
  -E TMPDIR=<스크래치>/tmp \
  -p WorkingDirectory=<워크트리> \
  <워크트리>/.venv/bin/python <명령>
```

순서는 다음과 같다. 앞 단계가 실패하면 다음 단계로 가지 않는다.

1. 판정기 점검: `scripts/search_baseline.py selftest`
2. S0 시험: `-m pytest tests/test_search_baseline.py -p no:cacheprovider --basetemp=<스크래치>/basetemp`
3. 코퍼스 생성: `scripts/search_corpus.py generate --preset S1 --seed 542 --out <스크래치>/corpus-S1`
4. 적재: `scripts/search_baseline.py load --corpus <스크래치>/corpus-S1 --work <스크래치>/work-S1`
5. 기본 설정 측정: `scripts/search_baseline.py run --corpus <스크래치>/corpus-S1 --work <스크래치>/work-S1 --run-dir <스크래치>/run-S1-default --config default --out <스크래치>/report-S1-default.json`
6. 축소 상한 측정: 5번에서 `--run-dir <스크래치>/run-S1-scaled --config scaled-cap --out <스크래치>/report-S1-scaled.json`으로 바꾼다.

판정 기준은 다음과 같다. 보고서의 `verdict`가 `COMPLETE`가 아니면 측정이 무효다. `COMPLETE`이면 `requirements`의 항목별 충족 여부가 4절 요구치의 판정이다. 질의별 분류는 `queries[*].legs[*].class`에 있다.

실행 중 자원 상한의 확인은 transient 서비스의 주 PID로 한다.

```bash
systemctl --user show <유닛 이름> -p MainPID
grep 'Max address space' /proc/<PID>/limits
cat /sys/fs/cgroup/$(cut -d: -f3 /proc/<PID>/cgroup)/cpu.max
```

하네스도 같은 값을 보고서 `environment`에 남긴다.
