# 마이그레이션 플랜: MCP 경유 적재 (Ingestion via MCP)

상태: 제안 (Proposed) · 최초 작성일: 2026-06-18 · 개정: 2026-07-03(post-chroma 3-백엔드 현행화) · 코드 구현 없음 (설계 문서)

> **개정 배경:** 최초 작성 시점엔 벡터 백엔드가 Chroma 하나뿐이었고, 이 문서의 핵심 동기는 "적재 시 MCP 중지" 운영 제약과 `chroma.lock(LOCK_EX)` 회피였다. 현재는 `VECTOR_BACKEND`가 `chroma`/`sqlite-vec`/`pgvector` 3종이고, **로컬 모드 기본값이 이미 `sqlite-vec`로 바뀌었다** — chroma는 이제 예외 경로(docker 모드, `EMBEDDING_BACKEND=local` 롤백, 명시적 오버라이드)다. §1이 이 현행 아키텍처를 먼저 설명하고, chroma 전용이었던 배경 서술은 §10(이력)으로 압축했다. §2 이후의 실질 설계(신규 MCP write 도구, 배치·purge-replace)는 벡터 백엔드와 무관하게 대부분 유효하다.

범위 확정: `pack_purge`(삭제) · `pack_ingest_chunks`(청크 배치) **두 신규 MCP write 도구 신설 포함**. `--fresh`(purge-replace)까지 MCP 무중단으로 달성한다.

관련 문서: `[[pgvector-migration-plan]]` (스토어 백엔드 교체·동시성 결정 힌지 — §9가 본 문서를 "실시간 동시 적재" 시나리오로 직접 인용), `[[vector-backends]]` (3-백엔드 매트릭스·기본값 해석 규칙)

---

## 1. 배경 / 현재 아키텍처

### 1.1 벡터 백엔드 3종과 기본값 (`opencrab/config.py` `Settings.vector_backend_resolved`)

```
VECTOR_BACKEND 명시됨?
  → 예: 그 값을 그대로 사용 (항상 최우선)
  → 아니오:
      STORAGE_MODE == "pg"                              → "pgvector"
      is_local(STORAGE_MODE in {local, kuzu}) AND
        EMBEDDING_BACKEND == "openai"(기본값)            → "sqlite-vec"
      그 외 (docker 모드이거나 EMBEDDING_BACKEND=local)   → "chroma"
```

즉 **아무 설정 없이 로컬 모드를 그대로 실행하면 `sqlite-vec`가 기본이다.** chroma는 이제 docker 모드 표준이거나 명시적 롤백(`EMBEDDING_BACKEND=local`/minilm, 또는 `VECTOR_BACKEND=chroma` 오버라이드)일 때만 선택된다. 상세 매트릭스는 `docs/vector-backends.md` §2-3 참고.

### 1.2 백엔드별 적재 동시성 현황

| 백엔드 | 적재 중 서빙 | 메커니즘 | 비고 |
|---|---|---|---|
| `chroma` | **불가** (MCP 중지 필요) | `PersistentClient`는 동일 persist 경로에 다중 프로세스 동시 쓰기 미지원 → `chroma.lock(LOCK_EX)` | 이제 로컬 chroma `PersistentClient` 를 여는 **모든** 소유자가 락을 잡는다 — `ChromaStore` 로컬 모드 인스턴스가 자기 수명 동안 보유하므로(#140) MCP·REST·CLI·마이그레이션이 함께 배제된다. docker 모드·레거시 롤백 한정 |
| `sqlite-vec` | **가능** (무중단) | graph/doc/sql과 동일한 SQLite **WAL** 규율. 리더는 락 없이 동시 진행, 라이터는 `busy_timeout(5s)`로 직렬화(`sqlite_vec_store.py` `_new_conn`) | **로컬 모드 신규 기본값**. 라이터는 여전히 **직렬화**(동시에 하나) — MVCC 아님 |
| `pgvector`(`STORAGE_MODE=pg`) | **가능** (진짜 다중 라이터) | PostgreSQL **MVCC** — 리더가 라이터를 막지 않고, 라이터끼리도 행 단위로만 경합 | `opencrab/stores/factory.py`가 graph/doc/sql/vector 4스토어 전부 `PGGraphStore`/`PgDocStore`/`SQLStore`/`PgVectorStore`로 PG에 통합 배치. `EMBEDDING_BACKEND=openai`(KURE) 필수 |

`pgvector-migration-plan.md` §9(동시성 결론 — 의사결정 힌지)는 본 문서를 "실시간 동시 적재가 확정 요구인 시나리오"로 직접 인용하며, 그 경우 sqlite-vec의 라이터 직렬화로는 부족하고 **pg 모드의 MVCC만이 근본 해法**이라고 결론짓는다. 반대로 현행처럼 "로더가 적재 시 사실상 단독 라이터"인 워크로드라면 sqlite-vec 직렬화로 충분하다. 즉:

- **가벼운 로컬 배포(현행 stop-to-load에 가까운 워크로드):** `sqlite-vec` 무중단 ingest로 충분 — 별도 인프라 없이 "MCP 중지" 절차만 제거.
- **진짜 동시 다중 라이터가 확정 요구(예: 백그라운드 로더 + 다수 MCP 클라이언트가 상시 동시 write, 또는 수백만 벡터 스케일):** `STORAGE_MODE=pg`로 이행 — 이것이 현재 아키텍처의 프로덕션/SaaS-스케일 답이다.
- **chroma:** docker 모드 표준이거나 명시적 롤백일 때만 남는 레거시 경로. 원래의 단일-라이터 제약과 `chroma.lock` 워크어라운드가 그대로 적용된다(§10 이력 참고).

### 1.3 그럼에도 MCP 경유 적재가 여전히 유효한 이유

`sqlite-vec`/`pg`가 스토어 계층의 동시성 문제를 이미 해소했다고 해서 본 문서의 목표가 사라지는 건 아니다. 남는 이유는 셋:

1. **로직 중복 제거.** 현행 로더 `load_local_packs.py`가 `make_*_store`+`OntologyBuilder`로 스토어를 직접 여는 경로가 `load_to_localcrab.py`(docker/Neo4j)와 중복 구현돼 있다(§1.5). MCP write 경로로 통일하면 스토어 API 변경 시 손봐야 할 지점이 하나로 준다.
2. **원자적 purge-replace가 지금 MCP에 없다.** `pack_ingest`는 있지만 팩 삭제(purge)는 MCP write 도구로 노출돼 있지 않다 — 백엔드와 무관하게 필요한 갭.
3. **chroma 경로(docker 모드)에는 여전히 구조적으로 필요.** chroma를 쓰는 한 "쓰기 프로세스는 MCP 하나뿐"이라는 구조가 유일한 해法이다.

반대로 **`_write_lock()`(write.lock)은 현재 백엔드에 관계없이 모든 write 도구에 무조건 적용된다**(`dispatch_tool`, `opencrab/mcp/tools.py` — `WRITE_TOOLS`에 속하면 `_write_lock()`으로 직렬화). `chroma.lock`은 이제 chroma 클라이언트를 여는 주체가 곧 잡는 주체이므로 백엔드 분기 자체가 필요없지만(#140), `write.lock` 직렬화는 여전히 백엔드 무관하게 걸린다 — pg 모드에서는 스토어가 MVCC로 진짜 동시 라이터를 지원함에도 MCP 계층에서 인위적으로 한 번에 하나만 쓰게 된다. 이는 본 계획의 결함이 아니라 **향후 최적화 여지**로 기록해 둔다(§6 단점).

---

## 1.5 적재 경로 인벤토리 / MCP 반영 범위

`~/opencrab-dump` 전수조사 결과 **적재/삭제 경로는 `load_local_packs.py` 하나가 아니다.** "MCP 수정 시 함께 반영" 범위를 분명히 하기 위해 경로를 정리한다.

| # | 경로 | 적재 방식 | 본 계획 범위 |
|---|---|---|---|
| ① | `load_local_packs.py` (외부 ops 스크립트, `~/opencrab-dump/scripts/ops/`) | 로컬 스토어 **직접** 적재(`make_*_store`+`OntologyBuilder`) + `--fresh` 삭제(`delete_pack`). 로더는 백엔드 무관 **무조건** `chroma.lock(LOCK_EX)`을 flock한다 — 다만 sqlite-vec/pg 백엔드에서는 `ChromaStore` 자체가 만들어지지 않아 아무도 이 락의 SH를 잡지 않으므로 EX 획득이 항상 즉시 성공해 **결과적으로 MCP 중지가 불필요**하다. chroma 백엔드일 때만 EX 실패 → MCP 중지 안내 경로가 발동한다 | **MCP 모드 전환 대상** |
| ② | 대화 reingest hook — `hooks/claude/localcrab-session-end.sh` + `hooks/claude/localcrab-lib.sh` | **이미 MCP `pack_ingest` 경유**(append-only, purge 없음). `lc_call`이 `initialize`→`tools/call` 핸드셰이크 + 3회 재시도 + outbox 재시도 큐 구현 | **호환성 회귀 검증 대상** (신규 도구가 기존 `pack_ingest` 동작을 깨지 않는지) |
| ③ | `load_to_localcrab.py` | Neo4j(STORAGE_MODE=docker) 벌크 적재. ①과 노드/엣지/청크 패스 **중복(복붙)** | **비목표**(별도 토폴로지, 영향분석만) |
| 보조 | `backfill_kure.py`(vector/doc upsert만), `dump_*conversations.py` 3종(jsonl 변환 전단계, 스토어 미접근) | — | **비목표** |
| 보조 | `scripts/migrate_chroma_to_sqlite_vec.py`, `migrate_sqlite_to_pg.py`, `migrate_add_binary_quantization.py`, `migrate_graph_to_ladybug.py` | 1회성 백엔드 이전 도구(재임베딩 불필요, 4스토어 1:1) | **비목표**(§9에서 포인터만) |

- **재사용 레퍼런스:** ②의 `lc_call`(`localcrab-lib.sh`)은 로더 MCP 클라이언트가 그대로 참고할 수 있는 기존 구현이다 — 특히 재시도·부분실패 처리·outbox 큐 패턴. (MCP HTTP가 stateless라 핸드셰이크 자체는 생략 가능하나 기존 hook과 동작 호환을 유지한다.)
- **중복 경고:** ①③이 적재 로직을 복제하므로 스토어 API 변경 시 두 곳을 각각 손봐야 한다. 본 계획은 **①만 MCP로 전환**하고, ②는 호환성만 검증하며, ③의 중복 통합은 별건으로 남긴다.

---

## 2. 목표 / 비목표

**목표**
- MCP 서버를 **중지하지 않고** 가동 중 적재를 수행한다 — chroma 경로에서는 필수, sqlite-vec/pg 경로에서는 로직 통일 + 원자적 purge-replace 확보가 실질 이득.
- **purge(기존 팩 삭제) 후 재적재까지** MCP 무중단으로 달성한다 — 현행 로더 `--fresh`와 동등한 "삭제 후 재적재" 워크플로를 MCP 경로로 지원한다. 백엔드 무관.
- chroma를 **서버 모드로 전환하지 않고도** 위를 달성한다(chroma 사용 시).
- 로더가 스토어를 직접 열지 않게 하여, 스토어 API 변경 시 손봐야 할 지점을 하나(MCP)로 좁힌다.

**비목표**
- 스토어 백엔드 교체 자체(Chroma → sqlite-vec/pgvector). → `[[pgvector-migration-plan]]`·`docs/vector-backends.md`에서 다룬다. 이미 마이그레이션 스크립트가 존재한다(§9).
- 임베딩 모델/청킹 전략 변경.
- MCP 인증 체계 재설계 (기존 Bearer 토큰 재사용).
- `write.lock`을 백엔드별로 조건부화(pg MVCC 활용)하는 것 — §1.3에서 향후 과제로만 기록.

---

## 3. 설계

핵심: 로더가 스토어를 **직접 열지 않는다.** 대신 가동 중인 MCP 서버의 **write 경로(도구)**를 호출한다. chroma 사용 시엔 이로써 스토어를 만지는 프로세스가 **MCP 하나뿐**이 되어 공식 제약(다중 프로세스 동시 쓰기 금지)을 구조적으로 위반하지 않는다(chroma 서버 모드 전환 불필요). sqlite-vec/pg 사용 시엔 이 구조가 필수는 아니지만 §1.3의 이유로 여전히 유효하다.

**호출 채널**
- 전송: 현재 Streamable HTTP MCP (`opencrab/cli.py serve --transport http`, 엔드포인트 `http://{host}:{port}/mcp`). 기본 포트는 config의 `mcp_http_port`(:8765).
- **Stateless 프로토콜**: 이 HTTP 전송은 의도적으로 **무상태**다 (`http_app.py` 모듈 docstring, `mcp_post`). `initialize`/`notifications/initialized` 핸드셰이크 없이도 각 POST가 독립적으로 `tools/call`을 처리한다(`server.py` `_dispatch`). 따라서 로더는 세션 관리 없이 httpx로 `{"jsonrpc":"2.0","method":"tools/call","params":{"name":...,"arguments":...},"id":1}`를 바로 POST하면 된다. **JSON-RPC 배열(배치)도 수용**되어 한 요청에 여러 `tools/call`을 담을 수 있다(`http_app.py`).
- **응답 형식**: 결과는 MCP content wrapper `{"content":[{"type":"text","text":<json 문자열>}]}`로 감싸진다(`server.py`). 또한 도구 내부 예외는 `{"error":...}`로 감싸져 **HTTP 200이어도 본문에 error가 들어올 수 있다**. `pack_ingest`류는 `node_errors`/`edge_errors` 리스트를 반환한다. → 로더는 content를 파싱해 `error`/부분 실패를 검사·재시도해야 한다.
- **기존 MCP 클라이언트 재사용**: 대화 reingest hook의 `lc_call`(`hooks/claude/localcrab-lib.sh`)이 MCP 호출 + 3회 재시도 + outbox 재시도 큐를 이미 구현한다(§1.5 ②). 로더 클라이언트는 이 재시도·부분실패·큐 패턴을 참고/공유해 중복 구현을 피한다.
- 인증(#145 이후): **사용자별 토큰**. `opencrab init` → `opencrab user add` → `opencrab token issue <user_id>` 로 발급하고 `Authorization: Bearer <token>` 로 제시한다. 서버는 `opencrab.auth.verify_token` 으로 `users`/`api_tokens` 를 조회해 검증한다. **무인증 모드와 공유 비밀 방식은 삭제됐다** — 관련 환경변수가 남아 있으면 기동을 거부한다. 헤더를 못 보내는 클라이언트용으로 `--allow-query-token`(기본 꺼짐)이 있다.
  - **인증 경로**: 무인증 인스턴스는 더 이상 존재하지 않는다. 로컬이든 원격이든 로더는 발급받은 토큰을 `Authorization: Bearer <token>` 헤더로 전달한다. 로컬 동일 호스트라면 stdio 전송이 더 단순하다 — 그쪽은 OS 프로세스 경계가 신뢰 근거이고 로컬 사용자로 자동 바인딩된다.

**재사용 가능한 기존 도구 시그니처** (`opencrab/mcp/tools.py`)
- `ontology_add_node(space, node_type, node_id, properties=None, tenant_id="default", subject_id=None)` — 건별 노드.
- `ontology_add_edge(from_space, from_id, relation, to_space, to_id, properties=None)` — 건별 엣지.
- `pack_ingest(pack_id, nodes=None, edges=None, text=None, title=None, source_id=None, text_as_node=True)` — **이미 `nodes`/`edges` 리스트를 배치로 받는다.** 단 청크/임베딩 배치 인자는 없다. 존재하지 않는 팩이면 에러 → `pack_create` 선행 필요.
  - ⚠️ **`text_as_node`는 청크 적재가 아니다.** 이 경로는 입력 `text`를 **문서 단위 evidence/TextUnit 노드 하나**로 물화할 뿐(`tools.py`), 로더 `load_chunks`가 만드는 **청크 단위 벡터 항목**과 다르다. 따라서 청크는 pack_ingest로 대체할 수 없고 전용 도구가 필요하다(§3.5, §4 참조).
- `pack_create(title, pack_id=None, description=None, nodes=None, edges=None, text=None, text_as_node=True)` — 신규 팩 생성. 필수는 `title`.

각 write 도구는 호출 시 `_write_lock()`으로 직렬화되고 `ctx["hybrid"].invalidate_bm25_cache()` 등 사후 처리를 수행하므로, 로더가 직접 빌더를 쓸 때와 동일한 정합성이 보장된다(백엔드 무관 — `SqliteVecStore`는 `ChromaStore`와 동일한 public 메서드/시그니처/반환값 계약을 지키도록 설계됐다, `sqlite_vec_store.py` 모듈 docstring "CONTRACT PARITY" 참고).

**로더 측 변경 (개념)**
- 현재 `OntologyBuilder` 직접 호출 지점(노드 `load_nodes`, 엣지 `load_edges`, 청크 `load_chunks`)을 MCP 클라이언트 호출로 치환하는 어댑터를 둔다. **임베딩은 서버측에서 계산되므로 로더는 텍스트만 전송**한다(chroma/sqlite-vec 모두 동일 — 앱측 임베딩 후 저장이라는 계약은 `sqlite_vec_store.py`가 chroma의 openai 경로를 그대로 따른다).
- **`id_map`은 로더가 입력 파일에서 직접 구축한다.** `load_nodes`가 이미 입력 row의 space/node_type을 정규화해 `id_map[node_id]=(space, node_type)`를 만들고(`load_local_packs.py:420`) 엣지 적재 시 조회한다(`456-464`). 즉 **서버 응답에 의존할 필요가 없다** — MCP 모드에서도 같은 입력 기반 맵을 유지하면 된다.
- `chroma.lock(LOCK_EX)` 획득 로직은 MCP 모드에서 **건너뛴다**(로더가 chroma를 직접 만지지 않으므로). 참고: 현행 로더는 백엔드 무관 무조건 flock하지만, sqlite-vec/pg에서는 `ChromaStore` 자체가 만들어지지 않아 아무도 SH를 잡지 않으므로(#140) 충돌이 발생하지 않는다.

---

## 3.5 신규 MCP 도구 스펙

두 신규 write 도구를 추가한다. 둘 다 서버측에서 임베딩을 계산하므로 클라이언트는 텍스트만 전달한다.

**`pack_purge(pack_id)`** — `--fresh`(삭제 후 재적재)의 삭제 절반을 담당. 로더 `delete_pack` 로직(`load_local_packs.py:313-361`)을 서버측 도구로 포팅한다.
- 동작: ① `graph.delete_node(node_type, node_id)`로 팩 소속 노드 + 연결 엣지 cascade 삭제(`local_graph_store.py`) → ② `docs.delete_node_doc(space, node_id)` + doc_sources DELETE(`local_sql_doc_store.py`) → ③ 청크 벡터 삭제. `vec.available`가 False면 벡터 단계 skip.
- ⚠️ **벡터 삭제 API는 백엔드마다 다르다.** `ChromaStore`는 메타데이터 `where` 기반 벌크 삭제(`_collection.delete(where={"pack_id": pack_id})`)를 지원하지만, `SqliteVecStore.delete(ids: list[str])`는 **id 리스트만** 받는다(where 인자 없음, `sqlite_vec_store.py`). 따라서 sqlite-vec/pgvector 대상일 때는 pack_purge가 먼저 `pack_id`로 id 목록을 조회(`SELECT node_id FROM {table} WHERE pack_id = ?` — 파티션 키라 저비용)한 뒤 `delete(ids=[...])`를 호출하는 어댑터가 필요하다. 구현 시 pgvector 쪽 삭제 API도 동일하게 확인할 것.
- 반환: 삭제된 노드/문서/벡터 수.

**`pack_ingest_chunks(pack_id, chunks=[{id, text, metadata}], batch_size=256)`** — 청크 단위 벡터 적재(로더 `load_chunks` 대응). pack_ingest의 노드 경로와 별개.
- 동작: 서버측 `vec.upsert_texts(texts, ids, metadatas)`(임베딩 서버 계산, chroma/sqlite-vec 공통 시그니처) + doc_sources upsert. 한 호출당 한 배치만 처리해 락 점유를 짧게 유지.
- 반환: 적재 청크 수 + 부분 실패 목록.

**등록 4지점** (`opencrab/mcp/tools.py`): ① 함수 정의 → ② `TOOL_SCHEMAS` 스키마 등록 → ③ `_TOOL_FUNCTIONS` 매핑 → ④ **`WRITE_TOOLS` 등록**(누락 시 `dispatch_tool`이 `_write_lock` 직렬화를 적용하지 않음). `TOOLS` 리스트는 `TOOL_SCHEMAS`에서 자동 생성된다. 두 도구 모두 **additive**라 기존 도구/직접 적재 경로에 영향이 없다.

---

## 4. 처리량 고려

현행 로더 적재 단위:
- 노드/엣지: **건별** (`load_nodes` / `load_edges`가 빌더를 항목마다 호출).
- 청크: **256 배치** (`flush()` + `batch_size`).

건별 HTTP 호출은 왕복 지연 때문에 느리다. 따라서 **배치 ingest 경로가 필요**하다.

- 노드/엣지: `pack_ingest(nodes=[...], edges=[...])`가 이미 리스트를 받으므로 활용. 엣지에 필요한 `id_map`은 **로더가 입력 파일에서 직접 구축**한다(§3 참조) — 서버 응답에 `(space, node_type)`을 돌려받을 필요가 없다.
- 청크/임베딩: **신규 전용 도구 `pack_ingest_chunks`를 사용**한다(§3.5). `pack_ingest`의 `text` 경로는 문서 단위 노드라 청크 대체 불가.

**MCP의 산발적 쓰기와의 공존**
- 적재 중에도 일반 사용자/에이전트가 가끔 write 도구를 호출할 수 있다. `_write_lock()`는 `LOCK_EX`이므로 한 번에 하나의 write만 진행된다 — **이는 백엔드가 pg(MVCC)여도 마찬가지다**(§1.3). 진짜 다중 라이터 처리량이 필요해지면 이 MCP 계층 직렬화가 다음 병목이 될 수 있다는 점을 염두에 둔다.
- **`_write_lock()`는 이미 `tools/call` 호출당 획득/해제된다**(`dispatch_tool`). 따라서 "한 호출 = 한 배치"로 설계하면 **배치 사이에 락이 자동으로 풀려** 로더 측에 별도 락 관리 코드가 필요 없다. 단 **거대한 단일 호출**(수만 노드를 한 JSON-RPC에 담음)은 한 락을 장시간 점유하므로, 클라이언트가 **배치 크기(예: 256)로 호출을 나눠** 산발 쓰기가 끼어들 틈을 준다.
- 요청 본문 크기/타임아웃에 코드상 명시 제한이 없다(FastAPI/uvicorn 기본). 큰 배치는 타임아웃·메모리 위험이 있으므로 배치 크기를 보수적으로 잡는다.
- 두 인스턴스(:8765 unauth / :8766 auth)는 **동일 `write.lock`을 공유**하므로, 로더 쓰기 중에는 양쪽 인스턴스의 모든 write가 직렬화된다(의도된 동작).
- **읽기는 락을 잡지 않으므로** 적재 내내 쿼리는 무중단으로 동작한다(chroma 사용 시에도 — chroma 쪽은 애초에 읽기·쓰기 모두 MCP 프로세스 내부이므로 스레드 세이프 보장 안에 있다).

**대량 팩 로드 효율 (적응형 배치)**

로더는 **한 번에 다수 팩(팩당 수천~수만 노드)**을 적재하므로, 건별 호출은 왕복 지연으로 비현실적이고 효율이 핵심 제약이다. 효율과 무중단 공존을 동시에 잡기 위해 **적응형 배치**를 채택한다.

- **기본은 큰 배치(고처리량):** HTTP 왕복 횟수와 `_write_lock()` 획득/해제 횟수를 줄여 처리량을 높인다.
- **산발 write 감지 시 배치 축소(무중단 양보):** 적재 중 다른 write가 지연되는 신호가 보이면 배치 크기를 줄여 락 점유 시간을 짧게 하고 산발 쓰기가 끼어들 틈을 넓힌다.
- **JSON-RPC 배치 배열로 왕복 절감:** 여러 `tools/call`을 **한 HTTP 요청의 JSON-RPC 배열**로 묶어 네트워크 왕복을 줄인다(`http_app.py`). 배열 내 각 항목은 항목별로 `_write_lock()`을 잡았다 풀므로(`server.handle_request`가 항목마다 디스패치) **락 점유는 여전히 짧게 유지**된다 — 처리량과 공존을 동시에 얻는다.
- **상한 주의:** 요청 본문 크기/타임아웃에 코드상 명시 제한이 없으므로(FastAPI/uvicorn 기본), 한 호출/배열이 지나치게 커지지 않도록 상한을 둔다(타임아웃·메모리·장시간 락 점유 회피).

---

## 5. 단계별 전환 계획

> 스토어를 만지는 작업이므로 **단계 0(백업)·단계 1(회귀 기준선 테스트)을 신규 코드 작성보다 먼저** 수행한다. 이는 선택이 아니라 필수 게이트다.

0. **DB 백업** (작업 전 1회)
   - `$LOCAL_DATA_DIR`(기본 `/home/asdf/.openclaw/workspace/data/localcrab`)를 `~/opencrab-dump/localcrab-backup/<YYYYMMDD-HHMMSS>/`로 스냅샷.
   - 대상: `graph.db`(+`-wal`/`-shm`), `doc_store.db`, `opencrab.db`(+wal/shm), 그리고 사용 중인 벡터 백엔드에 따라 `chroma/` 디렉터리 또는 `vectors.db`(+wal/shm). `write.lock`/`chroma.lock`은 제외.
   - WAL 일관성: MCP write 유휴 시점에 복사하거나 sqlite `.backup` 명령으로 일관 스냅샷을 권장.
1. **회귀 기준선 테스트** (신규 코드 작성 **전**)
   - 기존 경로의 **정상·실패·엣지** 케이스 characterization 테스트를 작성하고 `make test` 통과로 기준선 확보: `dispatch_tool`(미등록 도구 KeyError 등), `_write_lock` 직렬화, `pack_create`/`pack_ingest`, graph/doc/vec 삭제·upsert 경로. 벡터 백엔드는 로컬 기본값(sqlite-vec)과 chroma 양쪽에서 확인.
   - 기존 관례 재사용: `tests/test_service_paths_characterization.py`의 `local_env`/`local_stores`/`builder` 픽스처, `tmp_path`+`monkeypatch.setenv("LOCAL_DATA_DIR")` 격리, `tests/test_store_concurrency.py` 동시성 패턴.
2. **신규 write 도구 추가 + 신규 테스트**
   - `pack_purge`, `pack_ingest_chunks` 구현(§3.5, 벡터 삭제 API 백엔드 차이 어댑터 포함). 등록 4지점 — 특히 **`WRITE_TOOLS` 등록** 누락 주의.
   - 신규 테스트 **정상**(삭제/적재 성공), **실패**(없는 `pack_id`, `vec.available=False`, 빈 `chunks`), **엣지**(cascade 엣지 삭제, 중복 id upsert, 부분 실패 `node_errors`). `dispatch_tool("pack_purge"/"pack_ingest_chunks", …)`가 `_write_lock` 직렬화를 받는지 검증. sqlite-vec/chroma 양쪽 백엔드에서 pack_purge 반복 실행.
3. **로더에 MCP 클라이언트 모드 추가 + hook 호환성**
   - `--via-mcp`(가칭) 플래그 도입(로더는 argparse 미사용 → 수동 파싱부 `load_local_packs.py:689-704` 손봄). 켜지면 직접 스토어 생성 대신 MCP HTTP `tools/call` POST(§4 적응형 배치 + JSON-RPC 배치 배열, §1.5 ②의 `lc_call` 재시도 패턴 참고).
   - `--fresh`를 **MCP `pack_purge` 호출 후 ingest**로 매핑(삭제 절반을 MCP 경로로 수행).
   - 기존 직접 모드는 **기본값으로 보존** (플래그 미지정 시 현행과 동일). MCP 모드에서는 `chroma.lock` 획득을 건너뛰고(chroma 사용 시), 인증 인스턴스 사용 시에만 Bearer 토큰을 로드.
   - **대화 reingest hook(§1.5 ②) 회귀 검증:** 신규 도구 추가 후에도 `localcrab-session-end.sh`/`lc_call` 경유 `pack_ingest`(append-only)가 기존대로 동작하는지 확인 — hook 자체는 변경 없음.
4. **검증** (§7)
   - 회귀 무결성, 신규 기능 테스트, hook 호환성, MCP 가동 중 동시 적재, purge 후 재적재 정합성, 대량 적재 처리량 측정. sqlite-vec 기본값 + chroma 롤백 경로 양쪽 확인.
5. **기본 전환**
   - 검증 통과 후 `--via-mcp`를 기본 동작으로 승격. 직접 모드는 `--direct` 등으로 잔존(롤백/오프라인 대량 재적재용, chroma 사용 시 여전히 유의미).

> **범위 밖(영향분석만):** `load_to_localcrab.py`(Neo4j/docker)는 별도 토폴로지라 본 전환 대상이 아니다. 단 ①과 적재 로직을 복제하므로, 향후 스토어 API가 바뀌면 **이 파일도 함께 손봐야 하는 중복 지점**임을 기억한다(§1.5 중복 경고). `backfill_kure.py`도 동일.

---

## 6. 장단점

**장점**
- 신규 서버 프로세스(chroma 서버 모드)나 전면 재적재가 불필요 — **가장 가벼운 경로**.
- purge-replace까지 무중단으로 MCP 경로에 통합 — 현재 MCP에 없는 갭을 메운다(백엔드 무관).
- 로더가 스토어를 직접 열지 않으므로 스토어 API 변경 시 손볼 지점이 하나로 준다(§1.3).
- chroma 사용 시(docker 모드 등): "적재 시 MCP 중지" 운영 제약 제거, 다중 프로세스 제약을 구조적으로 회피(쓰기 프로세스가 MCP 1개).
- HTTP가 stateless라 로더 클라이언트가 단순(initialize 핸드셰이크 불필요). hook의 `lc_call` 재사용 가능.
- 로컬에서는 stdio 전송을 쓰면 토큰 처리가 불필요하다(로컬 사용자로 자동 바인딩).
- 적응형 배치 + JSON-RPC 배치 배열로 **대량 팩 로드 효율 확보**(왕복·락 횟수 절감, 짧은 락 점유 유지).

**단점**
- 건별/배치 모두 HTTP 왕복 + `_write_lock()` 직렬화 오버헤드 → 오프라인 직접 적재보다 느릴 수 있음.
- 청크 배치 도구 + **삭제 도구(`pack_purge`)** 신설 등 MCP 도구 추가 작업 필요. 벡터 삭제 API가 백엔드마다 달라(§3.5) 어댑터 코드가 필요.
- HTTP 전송을 쓰면 로더가 토큰 발급·배포를 다뤄야 한다(stdio 사용 시 불필요).
- 적재가 MCP 쓰기 락을 공유하므로, 락 점유 정책(배치 단위)을 잘못 잡으면 일반 쓰기 지연 가능. **pg 모드에서도 `write.lock`이 여전히 모든 write를 직렬화**하므로, MVCC가 주는 진짜 다중 라이터 이점을 MCP 계층이 상쇄한다(§1.3) — 이 직렬화를 백엔드별로 조건부화하는 건 별도 과제.
- 중복 적재 엔진 `load_to_localcrab.py`(Neo4j)는 MCP 전환에서 제외되어 **별도 유지보수로 남는다**(§1.5 중복 경고).

---

## 7. 검증 방법

- **회귀 무결성:** §5 단계 1 기준선 테스트가 신규 도구 추가 후에도 전부 통과(`make test` green)함을 확인 — 기존 동작 무회귀.
- **신규 기능 테스트:** `pack_purge`/`pack_ingest_chunks`의 정상·실패·엣지 케이스(§5 단계 2)가 전부 통과. sqlite-vec와 chroma 양쪽 백엔드에서.
- **hook 호환성:** 대화 reingest 경로(`localcrab-session-end.sh`/`lc_call` → `pack_ingest`, §1.5 ②)가 신규 도구 추가 후에도 기존대로 정상 적재되는지 확인.
- **동시 실행:** MCP 서버 가동 상태에서 로더(`--via-mcp`)를 실행한다. 적재 중 별도 클라이언트로 `ontology_query` / `opencrab_search_nodes` 등 **읽기 쿼리가 무중단**인지 확인.
- **쓰기 공존:** 적재 중 산발적 write 도구(예: `ontology_add_node`)를 호출해 배치 사이에 정상 처리되는지(과도한 블록 없음) 확인. purge와 읽기(`find_neighbors`/`ontology_query`) 동시 실행 시 데드락·정합성 확인(`tests/test_store_concurrency.py` 패턴).
- **데이터 정합성:** 동일 팩을 (a) 직접 모드, (b) MCP 모드로 각각 적재 후 노드/엣지/청크 수 및 샘플 내용이 일치하는지 비교. `id_map` 기반 엣지 연결이 누락 없이 재현되는지 확인.
- **purge 후 재적재 정합성:** 직접 모드 `--fresh` 결과와 MCP `pack_purge`+ingest 결과의 노드/엣지/청크 수·샘플이 일치하는지 비교.
- **응답 부분실패 검출:** content wrapper(`{"content":[{"text":…}]}`) 파싱 후 `error`/`node_errors`/`edge_errors`를 로더가 검출·재시도하는지 확인(HTTP 200이어도 본문 error 가능).
- **대량 적재 처리량:** 적응형 배치(큰 배치) vs 고정 256, JSON-RPC 배치 배열 유무별 적재 시간 측정. 다수 팩 동시 로드 시나리오에서 직접 모드 대비 회귀 폭 기록.
- **락 안전:** chroma 백엔드일 때 MCP 모드 적재 중 `chroma.lock(LOCK_EX)`를 잡지 않음을 확인(로더가 chroma 미접근). sqlite-vec/pg 백엔드일 때는 `ChromaStore` 자체가 만들어지지 않아 아무도 SH를 잡지 않으므로(#140) 로더의 무조건 flock과 충돌하지 않음을 확인.
- **백업 복구 리허설:** §5 단계 0 백업본으로 복원 시 적재 전 상태로 되돌아가는지 1회 확인.

---

## 8. 롤백

- `--via-mcp`를 끄고(또는 `--direct`) **직접 모드로 즉시 복귀**. 직접 모드는 단계 2~5 내내 보존되므로 코드 변경 없이 운영 절차만 되돌리면 된다.
- 신규 도구 2개(`pack_purge`, `pack_ingest_chunks`)는 추가 전용(additive)이라 기존 도구/직접 적재 경로에 영향 없음.
- 직접 모드 복귀 시(chroma 사용 환경이라면) 기존 "MCP 중지 → 적재 → 재시작" 절차(`chroma.lock(LOCK_EX)`)가 그대로 동작. sqlite-vec/pg 환경이라면 애초에 그 절차가 불필요.
- 데이터 손상 시 §5 단계 0의 `~/opencrab-dump/localcrab-backup/<타임스탬프>/` 스냅샷으로 `$LOCAL_DATA_DIR`(및 pg 사용 시 해당 DB)를 복원.

---

## 9. 벡터 백엔드 이전 경로 (포인터)

본 문서는 MCP 적재 경로 자체가 목적이라 백엔드 이전은 비목표다. chroma에서 벗어나려면 아래 1회성 스크립트를 사용한다(재임베딩 불필요, 상세는 각 문서 참고):

- `scripts/migrate_chroma_to_sqlite_vec.py` — chroma → sqlite-vec (로컬 모드 신규 기본값으로 전환).
- `scripts/migrate_sqlite_to_pg.py` — 4스토어(graph/doc/sql/vector) 1:1 이전, `STORAGE_MODE=pg`로 전환.
- `scripts/migrate_add_binary_quantization.py` — 기존 sqlite-vec DB에 binary 2단계 양자화 컬럼 백필(`VECTOR_ANN=binary`).
- `scripts/migrate_graph_to_ladybug.py` — 과거 그래프 스토어 kuzu → ladybug 이전 경로의
  read-only inspection 포인터. 현재 apply는 qualification 전까지 fixture-only이다.

상세 설계·동시성 결정 힌지는 `docs/pgvector-migration-plan.md` §8-9, 백엔드 조합 매트릭스는 `docs/vector-backends.md` 참고.

---

## 10. 이력 — Chroma 전용 시대의 배경 (참고용)

이 절은 2026-06-18 최초 작성 당시의 배경을 압축 보존한다. 현재는 §1.2에 따라 chroma가 예외 경로이므로 아래는 **chroma 사용 시에만 유효**하다.

- 현행 팩 로더 `/home/asdf/opencrab-dump/load_local_packs.py`는 로컬 스토어를 **직접 열어서** 적재했다. `make_graph_store`/`make_vector_store`/`make_doc_store`/`make_sql_store`로 스토어를 직접 생성하고 `OntologyBuilder(graph, docs, sql, vec=vec)`로 적재한다(라인 600-613). 임베딩은 **서버/빌더 측에서 계산된다**: `OntologyBuilder.add_node` 내부가 노드 텍스트를 추출해 `vec.upsert_texts(texts=[...])`로 넘기면 스토어가 임베딩한다(`opencrab/ontology/builder.py:148-166`).
- 적재 직전 `LOCAL_DATA_DIR/chroma.lock`에 `fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)`를 잡았다(라인 586-589). 선점 실패 시 즉시 종료하며, 안내 메시지가 **MCP 서버 중지를 요구**했다(라인 591-598): `systemctl --user stop localcrab-gateway` → 적재 → `systemctl --user start localcrab-gateway`.
- 이 배타 락이 필요했던 이유는 ChromaDB 제약 때문이다: `PersistentClient`는 **동일 persist 경로에 대한 다중 프로세스 동시 쓰기를 지원하지 않는다**(출처: Chroma Cookbook — System Constraints, "Chroma is not process-safe for concurrent writers sharing the same local persistence path." <https://cookbook.chromadb.dev/core/system_constraints/>). 단, **프로세스 내부 멀티스레드는 안전하다**("Chroma is thread-safe").
- MCP 서버 측은 이 제약을 락으로 방어했다(`opencrab/mcp/tools.py`): `_acquire_chroma_shared_lock()`가 서버 수명 동안 `chroma.lock`에 `LOCK_SH`를 보유 → 로더의 `LOCK_EX`와 상호 배제. uvicorn은 `workers=1`로 기동(`opencrab/cli.py`, 주석: 원래 "the chroma PersistentClient is single-process only") → chroma를 만지는 프로세스는 MCP 단일 인스턴스뿐. 여러 MCP 인스턴스(예: 인증/비인증 HTTP) 간 쓰기는 `_write_lock()`이 `write.lock`의 `LOCK_EX`로 직렬화. write 도구 집합은 `WRITE_TOOLS`: `ontology_add_node`, `ontology_add_edge`, `pack_create`, `pack_ingest`, `schema_pack_install`, `schema_pack_uninstall`, `harness_promotion_apply`.
- **이후 변화:** `_acquire_chroma_shared_lock()` 호출은 이제 `vector_backend_resolved == "chroma"`일 때만 실행되도록 조건부화됐고(`opencrab/mcp/tools.py` `_get_context()`), `cli.py`의 `workers=1` 주석도 "sqlite-vec(WAL) 하에서는 다중 워커가 기술적으로 가능하나 1을 유지" 식으로 갱신됐다. `write.lock` 직렬화 자체는 아직 백엔드 무관하게 남아 있다(§1.3). 이후 #140 이 그 조건부 획득마저 걷어냈다 — 잠금 소유가 MCP 도구 계층에서 `ChromaStore` 로컬 모드 인스턴스 수명으로 옮겨가, chroma 를 여는 주체가 곧 잠금을 잡는 주체가 됐다. 조건 분기가 필요없어진 이유는 chroma 백엔드가 아니면 `ChromaStore` 자체가 만들어지지 않기 때문이다.
- 이 락 문제를 **완전히** 해소한 것은 결국 스토어 계층의 재설계였다(sqlite-vec의 WAL 통일, pg의 MVCC) — MCP 라우팅은 chroma 시대엔 유일한 해法이었지만, 지금은 §1.3에 정리한 대로 보조적 이유로 유효성이 이동했다.
