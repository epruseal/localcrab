# 마이그레이션 플랜: MCP 경유 적재 (Ingestion via MCP)

상태: 제안 (Proposed) · 작성일: 2026-06-18 · 코드 구현 없음 (설계 문서)

> **⚠️ 갱신(2026-07-01):** 이 문서의 핵심 동기 중 하나였던 **"적재 시 MCP 중지" 운영 제약과 `chroma.lock(LOCK_EX)` 의존은, 벡터 백엔드를 `sqlite-vec`(vec0)로 라이브 전환하면서 이미 제거되었다**(`docs/pgvector-migration-plan.md` (A) 경로 라이브). 벡터가 SQLite WAL이 되어 적재 중에도 게이트웨이 무중단(로더 쓰기 ∥ serve 읽기, 라이터는 write.lock/busy_timeout 직렬화). 따라서 아래 §1의 "대량 재적재마다 MCP 중지" 부담 서술은 **chroma 백엔드 한정**이다. 다만 본 문서의 나머지 목표(`pack_purge`·`pack_ingest_chunks` MCP write 도구, 청크 단위 적재, 원자적 purge-replace)는 여전히 유효하다.

범위 확정: `pack_purge`(삭제) · `pack_ingest_chunks`(청크 배치) **두 신규 MCP write 도구 신설 포함**. `--fresh`(purge-replace)까지 MCP 무중단으로 달성한다.

관련 문서: `[[pgvector-migration-plan]]` (스토어 백엔드 교체 — 본 문서의 비목표)

---

## 1. 배경 / 문제

현행 팩 로더 `/home/asdf/opencrab-dump/load_local_packs.py` 는 로컬 스토어를 **직접 열어서** 적재한다.

- `make_graph_store` / `make_vector_store` / `make_doc_store` / `make_sql_store` 로 스토어를 직접 생성하고 `OntologyBuilder(graph, docs, sql, vec=vec)` 로 적재한다 (라인 600-613).
  - 임베딩은 **서버/빌더 측에서 계산된다**: `OntologyBuilder.add_node` 내부가 노드 텍스트를 추출해 `vec.upsert_texts(texts=[...])` 로 넘기면 ChromaStore 가 임베딩한다 (`opencrab/ontology/builder.py:148-166`). 즉 적재 호출자는 **원본 텍스트만** 넘기고 임베딩 벡터를 직접 만들지 않는다. 이 사실이 §3·§4 의 MCP 도구 설계 근거가 된다.
- 적재 직전 `LOCAL_DATA_DIR/chroma.lock` 에 `fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)` 를 잡는다 (라인 586-589). 선점 실패 시 즉시 종료하며, 안내 메시지가 **MCP 서버 중지를 요구**한다 (라인 591-598):
  - `systemctl --user stop localcrab-gateway` → 적재 → `systemctl --user start localcrab-gateway`

이 배타 락이 필요한 이유는 ChromaDB 제약 때문이다.

- ChromaDB `PersistentClient` 는 **동일 persist 경로에 대한 다중 프로세스 동시 쓰기를 지원하지 않는다.**
  - 출처: Chroma Cookbook — System Constraints, "Chroma is not process-safe for concurrent writers sharing the same local persistence path." <https://cookbook.chromadb.dev/core/system_constraints/>
- 단, **프로세스 내부 멀티스레드는 안전하다** ("Chroma is thread-safe").

MCP 서버 측은 이 제약을 락으로 방어한다 (`opencrab/mcp/tools.py`).

- `_acquire_chroma_shared_lock()` 가 서버 수명 동안 `chroma.lock` 에 `LOCK_SH` 를 보유 (라인 59-64) → 로더의 `LOCK_EX` 와 상호 배제.
- uvicorn 은 `workers=1` 로 기동 (`opencrab/cli.py` 라인 158-162, 주석: "the chroma PersistentClient is single-process only") → chroma 를 만지는 프로세스는 MCP 단일 인스턴스뿐.
- 여러 MCP 인스턴스(예: 인증/비인증 HTTP) 간 쓰기는 `_write_lock()` 이 `write.lock` 의 `LOCK_EX` 로 직렬화 (라인 84-95). write 도구 집합은 `WRITE_TOOLS` (라인 73-81): `ontology_add_node`, `ontology_add_edge`, `pack_create`, `pack_ingest`, `schema_pack_install`, `schema_pack_uninstall`, `harness_promotion_apply`.

**운영 부담:** 대량 재적재마다 MCP 서버를 중지해야 하므로, 적재 동안 모든 읽기 쿼리/도구가 중단된다.

---

## 1.5 적재 경로 인벤토리 / MCP 반영 범위

`~/opencrab-dump` 전수조사 결과 **적재/삭제 경로는 `load_local_packs.py` 하나가 아니다.** "MCP 수정 시 함께 반영" 범위를 분명히 하기 위해 경로를 정리한다.

| # | 경로 | 적재 방식 | 본 계획 범위 |
|---|---|---|---|
| ① | `load_local_packs.py` | 로컬 chroma/SQLite **직접** 적재(`make_*_store`+`OntologyBuilder`) + `--fresh` 삭제(`delete_pack`, 라인 313-361) | **MCP 모드 전환 대상** |
| ② | 대화 reingest hook — `hooks/claude/localcrab-session-end.sh` + `hooks/claude/localcrab-lib.sh` | **이미 MCP `pack_ingest` 경유**(append-only, purge 없음). `lc_call` 이 `initialize`→`tools/call` 핸드셰이크 + 3회 재시도 + outbox 재시도 큐 구현 | **호환성 회귀 검증 대상** (신규 도구가 기존 `pack_ingest` 동작을 깨지 않는지) |
| ③ | `load_to_localcrab.py` | Neo4j(STORAGE_MODE=docker) 벌크 적재. ①과 노드/엣지/청크 패스 **중복(복붙)** | **비목표**(별도 토폴로지, 영향분석만) |
| 보조 | `backfill_kure.py`(vector/doc upsert만), `dump_*conversations.py` 3종(jsonl 변환 전단계, 스토어 미접근) | — | **비목표** |

- **재사용 레퍼런스:** ②의 `lc_call`(`localcrab-lib.sh`)은 로더 MCP 클라이언트가 그대로 참고할 수 있는 기존 구현이다 — 특히 재시도·부분실패 처리·outbox 큐 패턴. (MCP HTTP 가 stateless 라 핸드셰이크 자체는 생략 가능하나 기존 hook 과 동작 호환을 유지한다.)
- **중복 경고:** ①③ 가 적재 로직을 복제하므로 스토어 API 변경 시 두 곳을 각각 손봐야 한다. 본 계획은 **①만 MCP 로 전환**하고, ②는 호환성만 검증하며, ③의 중복 통합은 별건으로 남긴다.

---

## 2. 목표 / 비목표

**목표**
- MCP 서버를 **중지하지 않고** 가동 중 적재를 수행한다.
- **purge(기존 팩 삭제) 후 재적재까지** MCP 무중단으로 달성한다 — 현행 로더 `--fresh` 와 동등한 "삭제 후 재적재" 워크플로(메모리상 purge-replace 가 실제 use case)를 MCP 경로로 지원한다.
- "적재 시 MCP 중지" 운영 제약 및 `chroma.lock(LOCK_EX)` 의존을 제거한다.
- chroma 를 **서버 모드로 전환하지 않고도** 위를 달성한다.

**비목표**
- 스토어 백엔드 교체 (Chroma → pgvector 등). → `[[pgvector-migration-plan]]` 에서 다룬다.
- 임베딩 모델/청킹 전략 변경.
- MCP 인증 체계 재설계 (기존 Bearer 토큰 재사용).

---

## 3. 설계

핵심: 로더가 스토어를 **직접 열지 않는다.** 대신 가동 중인 MCP 서버의 **write 경로(도구)** 를 호출한다. 그러면 chroma/스토어를 만지는 프로세스가 **MCP 하나뿐**이 되어 공식 제약(다중 프로세스 동시 쓰기 금지)을 구조적으로 위반하지 않는다. chroma 서버 모드 전환이 불필요하다.

**호출 채널**
- 전송: 현재 Streamable HTTP MCP (`opencrab/cli.py serve --transport http`, 엔드포인트 `http://{host}:{port}/mcp`). 기본 포트는 config 의 `mcp_http_port`(:8765).
- **Stateless 프로토콜**: 이 HTTP 전송은 의도적으로 **무상태**다 (`http_app.py` 모듈 docstring, `mcp_post` 라인 79-98). `initialize`/`notifications/initialized` 핸드셰이크 없이도 각 POST 가 독립적으로 `tools/call` 을 처리한다 (`server.py` `_dispatch` 라인 136-151). 따라서 로더는 세션 관리 없이 httpx 로 `{"jsonrpc":"2.0","method":"tools/call","params":{"name":...,"arguments":...},"id":1}` 를 바로 POST 하면 된다. **JSON-RPC 배열(배치)도 수용**되어 한 요청에 여러 `tools/call` 을 담을 수 있다 (`http_app.py:92-95`).
- **응답 형식**: 결과는 MCP content wrapper `{"content":[{"type":"text","text":<json 문자열>}]}` 로 감싸진다 (`server.py:209-216`). 또한 도구 내부 예외는 `{"error":...}` 로 감싸져 **HTTP 200 이어도 본문에 error 가 들어올 수 있다** (`server.py:204-207`). `pack_ingest` 류는 `node_errors`/`edge_errors` 리스트를 반환한다. → 로더는 content 를 파싱해 `error`/부분 실패를 검사·재시도해야 한다.
- **기존 MCP 클라이언트 재사용**: 대화 reingest hook 의 `lc_call`(`hooks/claude/localcrab-lib.sh`)이 MCP 호출 + 3회 재시도 + outbox 재시도 큐를 이미 구현한다(§1.5 ②). 로더 클라이언트는 이 재시도·부분실패·큐 패턴을 참고/공유해 중복 구현을 피한다.
- 인증: Bearer 토큰. `--auth-token` 또는 `--auth-token-file` 로 주입되며 `_resolve_token()` 으로 해석 (정의는 `opencrab/mcp/http_app.py:34-50`; `cli.py:143` 은 호출 지점). 우선순위: CLI 토큰 > `LOCALCRAB_MCP_TOKEN` env > 토큰 파일. 토큰이 없으면 `OPEN(no-auth)`. 검증은 `hmac.compare_digest` (`http_app.py:65-77`).
  - **인증 경로 선택**: 로더가 **로컬 동일 호스트**에서 돌면 무인증 인스턴스(:8765)로 붙어 Bearer 처리를 생략할 수 있다. 외부/인증 인스턴스(:8766)를 쓸 때만 토큰 파일을 로드해 `Authorization: Bearer <token>` 헤더로 전달한다.

**재사용 가능한 기존 도구 시그니처** (`opencrab/mcp/tools.py`)
- `ontology_add_node(space, node_type, node_id, properties=None, tenant_id="default", subject_id=None)` (라인 198) — 건별 노드.
- `ontology_add_edge(from_space, from_id, relation, to_space, to_id, properties=None)` (라인 250) — 건별 엣지.
- `pack_ingest(pack_id, nodes=None, edges=None, text=None, title=None, source_id=None, text_as_node=True)` (라인 1466) — **이미 `nodes`/`edges` 리스트를 배치로 받는다.** 단 청크/임베딩 배치 인자는 없다. 존재하지 않는 팩이면 에러 → `pack_create` 선행 필요.
  - ⚠️ **`text_as_node` 는 청크 적재가 아니다.** 이 경로는 입력 `text` 를 **문서 단위 evidence/TextUnit 노드 하나**로 물화할 뿐(`tools.py:1332-1358`), 로더 `load_chunks` 가 만드는 **청크 단위 벡터 항목**과 다르다. 따라서 청크는 pack_ingest 로 대체할 수 없고 전용 도구가 필요하다(§3.5, §4 참조).
- `pack_create(title, pack_id=None, description=None, nodes=None, edges=None, text=None, text_as_node=True)` (라인 1393) — 신규 팩 생성. 필수는 `title`.

각 write 도구는 호출 시 `_write_lock()` 로 직렬화되고 `ctx["hybrid"].invalidate_bm25_cache()` 등 사후 처리를 수행하므로, 로더가 직접 빌더를 쓸 때와 동일한 정합성이 보장된다.

**로더 측 변경 (개념)**
- 현재 `OntologyBuilder` 직접 호출 지점(노드 `load_nodes`, 엣지 `load_edges`, 청크 `load_chunks`)을 MCP 클라이언트 호출로 치환하는 어댑터를 둔다. **임베딩은 서버측에서 계산되므로 로더는 텍스트만 전송**한다.
- **`id_map` 은 로더가 입력 파일에서 직접 구축한다.** `load_nodes` 가 이미 입력 row 의 space/node_type 을 정규화해 `id_map[node_id]=(space, node_type)` 를 만들고(`load_local_packs.py:420`) 엣지 적재 시 조회한다(`456-464`). 즉 **서버 응답에 의존할 필요가 없다** — MCP 모드에서도 같은 입력 기반 맵을 유지하면 된다.
- `chroma.lock(LOCK_EX)` 획득 로직(라인 585-598)은 MCP 모드에서 **건너뛴다** (로더가 chroma 를 직접 만지지 않으므로).

---

## 3.5 신규 MCP 도구 스펙

두 신규 write 도구를 추가한다. 둘 다 서버측에서 임베딩을 계산하므로 클라이언트는 텍스트만 전달한다.

**`pack_purge(pack_id)`** — `--fresh`(삭제 후 재적재)의 삭제 절반을 담당. 로더 `delete_pack` 로직(`load_local_packs.py:313-361`)을 서버측 도구로 포팅한다.
- 동작: ① `graph.delete_node(node_type, node_id)` 로 팩 소속 노드 + 연결 엣지 cascade 삭제(`local_graph_store.py:249-263`) → ② `docs.delete_node_doc(space, node_id)` + doc_sources DELETE(`local_sql_doc_store.py:262-276`) → ③ `vec.delete(ids=[...])`(또는 `vec._collection.delete(where={"source": pack_id})`)로 청크 벡터 삭제(`chroma_store.py:237-241`). `vec.available` 가 False 면 벡터 단계 skip.
- 반환: 삭제된 노드/문서/벡터 수.

**`pack_ingest_chunks(pack_id, chunks=[{id, text, metadata}], batch_size=256)`** — 청크 단위 벡터 적재(로더 `load_chunks` 대응). pack_ingest 의 노드 경로와 별개.
- 동작: 서버측 `vec.upsert_texts(texts, ids, metadatas)`(`chroma_store.py:151-170`, 임베딩 서버 계산) + doc_sources upsert. 한 호출당 한 배치만 처리해 락 점유를 짧게 유지.
- 반환: 적재 청크 수 + 부분 실패 목록.

**등록 4지점** (`opencrab/mcp/tools.py`): ① 함수 정의 → ② `TOOL_SCHEMAS` 스키마 등록 → ③ `_TOOL_FUNCTIONS` 매핑 → ④ **`WRITE_TOOLS` 등록**(누락 시 `dispatch_tool` 이 `_write_lock` 직렬화를 적용하지 않음, 라인 2270). `TOOLS` 리스트는 `TOOL_SCHEMAS` 에서 자동 생성된다. 두 도구 모두 **additive** 라 기존 도구/직접 적재 경로에 영향이 없다.

---

## 4. 처리량 고려

현행 로더 적재 단위:
- 노드/엣지: **건별** (`load_nodes` / `load_edges` 가 빌더를 항목마다 호출).
- 청크: **256 배치** (`flush()` + `batch_size`, 라인 575-578).

건별 HTTP 호출은 왕복 지연 때문에 느리다. 따라서 **배치 ingest 경로가 필요**하다.

- 노드/엣지: `pack_ingest(nodes=[...], edges=[...])` 가 이미 리스트를 받으므로 활용. 엣지에 필요한 `id_map` 은 **로더가 입력 파일에서 직접 구축**한다(§3 참조, `load_local_packs.py:420`) — 서버 응답에 `(space, node_type)` 을 돌려받을 필요가 없다.
- 청크/임베딩: **신규 전용 도구 `pack_ingest_chunks` 를 사용**한다(§3.5). `pack_ingest` 의 `text` 경로는 문서 단위 노드라 청크 대체 불가.

**MCP 의 산발적 쓰기와의 공존**
- 적재 중에도 일반 사용자/에이전트가 가끔 write 도구를 호출할 수 있다. `_write_lock()` 는 `LOCK_EX` 이므로 한 번에 하나의 write 만 진행된다.
- **`_write_lock()` 는 이미 `tools/call` 호출당 획득/해제된다**(`dispatch_tool`, `tools.py:2270-2272`). 따라서 "한 호출 = 한 배치" 로 설계하면 **배치 사이에 락이 자동으로 풀려** 로더 측에 별도 락 관리 코드가 필요 없다. 단 **거대한 단일 호출**(수만 노드를 한 JSON-RPC 에 담음)은 한 락을 장시간 점유하므로, 클라이언트가 **배치 크기(예: 256)로 호출을 나눠** 산발 쓰기가 끼어들 틈을 준다.
- 요청 본문 크기/타임아웃에 코드상 명시 제한이 없다(FastAPI/uvicorn 기본). 큰 배치는 타임아웃·메모리 위험이 있으므로 배치 크기를 보수적으로 잡는다.
- 두 인스턴스(:8765 unauth / :8766 auth)는 **동일 `write.lock` 을 공유**하므로, 로더 쓰기 중에는 양쪽 인스턴스의 모든 write 가 직렬화된다(의도된 동작).
- **읽기는 락을 잡지 않으므로** 적재 내내 쿼리는 무중단으로 동작한다.

**대량 팩 로드 효율 (적응형 배치)**

로더는 **한 번에 다수 팩(팩당 수천~수만 노드)** 을 적재하므로, 건별 호출은 왕복 지연으로 비현실적이고 효율이 핵심 제약이다. 효율과 무중단 공존을 동시에 잡기 위해 **적응형 배치**를 채택한다.

- **기본은 큰 배치(고처리량):** HTTP 왕복 횟수와 `_write_lock()` 획득/해제 횟수를 줄여 처리량을 높인다.
- **산발 write 감지 시 배치 축소(무중단 양보):** 적재 중 다른 write 가 지연되는 신호가 보이면 배치 크기를 줄여 락 점유 시간을 짧게 하고 산발 쓰기가 끼어들 틈을 넓힌다.
- **JSON-RPC 배치 배열로 왕복 절감:** 여러 `tools/call` 을 **한 HTTP 요청의 JSON-RPC 배열**로 묶어 네트워크 왕복을 줄인다(`http_app.py:92-95`). 배열 내 각 항목은 항목별로 `_write_lock()` 을 잡았다 풀므로(`server.handle_request` 가 항목마다 디스패치) **락 점유는 여전히 짧게 유지**된다 — 처리량과 공존을 동시에 얻는다.
- **상한 주의:** 요청 본문 크기/타임아웃에 코드상 명시 제한이 없으므로(FastAPI/uvicorn 기본), 한 호출/배열이 지나치게 커지지 않도록 상한을 둔다(타임아웃·메모리·장시간 락 점유 회피).

---

## 5. 단계별 전환 계획

> 스토어를 만지는 작업이므로 **단계 0(백업)·단계 1(회귀 기준선 테스트)을 신규 코드 작성보다 먼저** 수행한다. 이는 선택이 아니라 필수 게이트다.

0. **DB 백업** (작업 전 1회)
   - `$LOCAL_DATA_DIR`(기본 `/home/asdf/.openclaw/workspace/data/localcrab`)를 `~/opencrab-dump/localcrab-backup/<YYYYMMDD-HHMMSS>/` 로 스냅샷.
   - 대상: `graph.db`(+`-wal`/`-shm`), `doc_store.db`, `opencrab.db`(+wal/shm), `chroma/` 디렉토리. `write.lock`/`chroma.lock` 은 제외.
   - WAL 일관성: MCP write 유휴 시점에 복사하거나 sqlite `.backup` 명령으로 일관 스냅샷을 권장.
1. **회귀 기준선 테스트** (신규 코드 작성 **전**)
   - 기존 경로의 **정상·실패·엣지** 케이스 characterization 테스트를 작성하고 `make test` 통과로 기준선 확보: `dispatch_tool`(미등록 도구 KeyError 등), `_write_lock` 직렬화, `pack_create`/`pack_ingest`, graph/doc/vec 삭제·upsert 경로.
   - 기존 관례 재사용: `tests/test_service_paths_characterization.py` 의 `local_env`/`local_stores`/`builder` 픽스처, `tmp_path`+`monkeypatch.setenv("LOCAL_DATA_DIR")` 격리, `tests/test_store_concurrency.py` 동시성 패턴.
2. **신규 write 도구 추가 + 신규 테스트**
   - `pack_purge`, `pack_ingest_chunks` 구현(§3.5). 등록 4지점 — 특히 **`WRITE_TOOLS` 등록** 누락 주의.
   - 신규 테스트 **정상**(삭제/적재 성공), **실패**(없는 `pack_id`, `vec.available=False`, 빈 `chunks`), **엣지**(cascade 엣지 삭제, 중복 id upsert, 부분 실패 `node_errors`). `dispatch_tool("pack_purge"/"pack_ingest_chunks", …)` 가 `_write_lock` 직렬화를 받는지 검증.
3. **로더에 MCP 클라이언트 모드 추가 + hook 호환성**
   - `--via-mcp`(가칭) 플래그 도입(로더는 argparse 미사용 → 수동 파싱부 `load_local_packs.py:689-704` 손봄). 켜지면 직접 스토어 생성 대신 MCP HTTP `tools/call` POST(§4 적응형 배치 + JSON-RPC 배치 배열, §1.5 ②의 `lc_call` 재시도 패턴 참고).
   - `--fresh` 를 **MCP `pack_purge` 호출 후 ingest** 로 매핑(삭제 절반을 MCP 경로로 수행).
   - 기존 직접 모드는 **기본값으로 보존** (플래그 미지정 시 현행과 동일, `chroma.lock(LOCK_EX)` 유지). MCP 모드에서는 `chroma.lock` 획득을 건너뛰고, 인증 인스턴스 사용 시에만 Bearer 토큰을 로드.
   - **대화 reingest hook(§1.5 ②) 회귀 검증:** 신규 도구 추가 후에도 `localcrab-session-end.sh`/`lc_call` 경유 `pack_ingest`(append-only) 가 기존대로 동작하는지 확인 — hook 자체는 변경 없음.
4. **검증** (§7)
   - 회귀 무결성, 신규 기능 테스트, hook 호환성, MCP 가동 중 동시 적재, purge 후 재적재 정합성, 대량 적재 처리량 측정.
5. **기본 전환**
   - 검증 통과 후 `--via-mcp` 를 기본 동작으로 승격. 직접 모드는 `--direct` 등으로 잔존(롤백/오프라인 대량 재적재용).

> **범위 밖(영향분석만):** `load_to_localcrab.py`(Neo4j/docker) 는 별도 토폴로지라 본 전환 대상이 아니다. 단 ①과 적재 로직을 복제하므로, 향후 스토어 API 가 바뀌면 **이 파일도 함께 손봐야 하는 중복 지점**임을 기억한다(§1.5 중복 경고). `backfill_kure.py` 도 동일.

---

## 6. 장단점

**장점**
- 신규 서버 프로세스(chroma 서버 모드)나 전면 재적재가 불필요 — **가장 가벼운 경로**.
- "적재 시 MCP 중지" 운영 제약 제거, 읽기 무중단. purge-replace 까지 무중단.
- chroma 다중 프로세스 제약을 구조적으로 회피 (쓰기 프로세스가 MCP 1개).
- HTTP 가 stateless 라 로더 클라이언트가 단순(initialize 핸드셰이크 불필요). hook 의 `lc_call` 재사용 가능.
- 로컬 무인증 인스턴스(:8765) 사용 시 **Bearer 토큰 처리를 생략** 가능.
- 적응형 배치 + JSON-RPC 배치 배열로 **대량 팩 로드 효율 확보**(왕복·락 횟수 절감, 짧은 락 점유 유지).

**단점**
- 건별/배치 모두 HTTP 왕복 + `_write_lock()` 직렬화 오버헤드 → 오프라인 직접 적재보다 느릴 수 있음.
- 청크 배치 도구 + **삭제 도구(`pack_purge`)** 신설 등 MCP 도구 추가 작업 필요.
- 인증 인스턴스(:8766) 사용 시 Bearer 토큰 인증/배포를 로더가 다뤄야 함(로컬 무인증 인스턴스 사용 시 완화).
- 적재가 MCP 쓰기 락을 공유하므로, 락 점유 정책(배치 단위)을 잘못 잡으면 일반 쓰기 지연 가능.
- 중복 적재 엔진 `load_to_localcrab.py`(Neo4j) 는 MCP 전환에서 제외되어 **별도 유지보수로 남는다**(§1.5 중복 경고).

---

## 7. 검증 방법

- **회귀 무결성:** §5 단계 1 기준선 테스트가 신규 도구 추가 후에도 전부 통과(`make test` green)함을 확인 — 기존 동작 무회귀.
- **신규 기능 테스트:** `pack_purge`/`pack_ingest_chunks` 의 정상·실패·엣지 케이스(§5 단계 2)가 전부 통과.
- **hook 호환성:** 대화 reingest 경로(`localcrab-session-end.sh`/`lc_call` → `pack_ingest`, §1.5 ②)가 신규 도구 추가 후에도 기존대로 정상 적재되는지 확인.
- **동시 실행:** MCP 서버 가동 상태에서 로더(`--via-mcp`)를 실행한다. 적재 중 별도 클라이언트로 `ontology_query` / `opencrab_search_nodes` 등 **읽기 쿼리가 무중단**인지 확인.
- **쓰기 공존:** 적재 중 산발적 write 도구(예: `ontology_add_node`)를 호출해 배치 사이에 정상 처리되는지(과도한 블록 없음) 확인. purge 와 읽기(`find_neighbors`/`ontology_query`) 동시 실행 시 데드락·정합성 확인(`tests/test_store_concurrency.py` 패턴).
- **데이터 정합성:** 동일 팩을 (a) 직접 모드, (b) MCP 모드로 각각 적재 후 노드/엣지/청크 수 및 샘플 내용이 일치하는지 비교. `id_map` 기반 엣지 연결이 누락 없이 재현되는지 확인.
- **purge 후 재적재 정합성:** 직접 모드 `--fresh` 결과와 MCP `pack_purge`+ingest 결과의 노드/엣지/청크 수·샘플이 일치하는지 비교.
- **응답 부분실패 검출:** content wrapper(`{"content":[{"text":…}]}`) 파싱 후 `error`/`node_errors`/`edge_errors` 를 로더가 검출·재시도하는지 확인(HTTP 200 이어도 본문 error 가능).
- **대량 적재 처리량:** 적응형 배치(큰 배치) vs 고정 256, JSON-RPC 배치 배열 유무별 적재 시간 측정. 다수 팩 동시 로드 시나리오에서 직접 모드 대비 회귀 폭 기록.
- **락 안전:** MCP 모드 적재 중 `chroma.lock(LOCK_EX)` 를 잡지 않음을 확인(로더가 chroma 미접근).
- **백업 복구 리허설:** §5 단계 0 백업본으로 복원 시 적재 전 상태로 되돌아가는지 1회 확인.

---

## 8. 롤백

- `--via-mcp` 를 끄고(또는 `--direct`) **직접 모드로 즉시 복귀**. 직접 모드는 단계 2~5 내내 보존되므로 코드 변경 없이 운영 절차만 되돌리면 된다.
- 신규 도구 2개(`pack_purge`, `pack_ingest_chunks`)는 추가 전용(additive)이라 기존 도구/직접 적재 경로에 영향 없음.
- 직접 모드 복귀 시 기존 "MCP 중지 → 적재 → 재시작" 절차(`chroma.lock(LOCK_EX)`)가 그대로 동작.
- 데이터 손상 시 §5 단계 0 의 `~/opencrab-dump/localcrab-backup/<타임스탬프>/` 스냅샷으로 `$LOCAL_DATA_DIR` 를 복원.
