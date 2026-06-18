# 마이그레이션 플랜: MCP 경유 적재 (Ingestion via MCP)

상태: 제안 (Proposed) · 작성일: 2026-06-18 · 코드 구현 없음 (설계 문서)

관련 문서: `[[pgvector-migration-plan]]` (스토어 백엔드 교체 — 본 문서의 비목표)

---

## 1. 배경 / 문제

현행 팩 로더 `/home/asdf/opencrab-dump/load_local_packs.py` 는 로컬 스토어를 **직접 열어서** 적재한다.

- `make_graph_store` / `make_vector_store` / `make_doc_store` / `make_sql_store` 로 스토어를 직접 생성하고 `OntologyBuilder(graph, docs, sql, vec=vector)` 로 적재한다 (라인 600-613).
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

## 2. 목표 / 비목표

**목표**
- MCP 서버를 **중지하지 않고** 가동 중 적재를 수행한다.
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
- 인증: Bearer 토큰. `--auth-token` 또는 `--auth-token-file` 로 주입되며 `_resolve_token()` 으로 해석 (`cli.py` 라인 115-143). 토큰이 없으면 `OPEN(no-auth)`. 로더는 토큰 파일 경로를 읽어 헤더로 전달.

**재사용 가능한 기존 도구 시그니처** (`opencrab/mcp/tools.py`)
- `ontology_add_node(space, node_type, node_id, properties=None, tenant_id="default", subject_id=None)` (라인 198) — 건별 노드.
- `ontology_add_edge(from_space, from_id, relation, to_space, to_id, properties=None)` (라인 250) — 건별 엣지.
- `pack_ingest(pack_id, nodes=None, edges=None, text=None, title=None, source_id=None, text_as_node=True)` (라인 1466) — **이미 `nodes`/`edges` 리스트를 배치로 받는다.** 단 청크/임베딩 배치 인자는 없다. 존재하지 않는 팩이면 에러 → `pack_create` 선행 필요.

각 write 도구는 호출 시 `_write_lock()` 로 직렬화되고 `ctx["hybrid"].invalidate_bm25_cache()` 등 사후 처리를 수행하므로, 로더가 직접 빌더를 쓸 때와 동일한 정합성이 보장된다.

**로더 측 변경 (개념)**
- 현재 `OntologyBuilder` 직접 호출 지점(노드 `load_nodes`, 엣지 `load_edges`, 청크 `load_chunks`)을 MCP 클라이언트 호출로 치환하는 어댑터를 둔다.
- `chroma.lock(LOCK_EX)` 획득 로직(라인 585-598)은 MCP 모드에서 **건너뛴다** (로더가 chroma 를 직접 만지지 않으므로).

---

## 4. 처리량 고려

현행 로더 적재 단위:
- 노드/엣지: **건별** (`load_nodes` / `load_edges` 가 빌더를 항목마다 호출).
- 청크: **256 배치** (`flush()` + `batch_size`, 라인 575-578).

건별 HTTP 호출은 왕복 지연 때문에 느리다. 따라서 **배치 ingest 경로가 필요**하다.

- 노드/엣지: `pack_ingest(nodes=[...], edges=[...])` 가 이미 리스트를 받으므로 활용 가능. 다만 로더의 통합 `id_map`(노드 적재 중 `id→(space, node_type)` 맵 구축, 라인 624-642)이 엣지 적재에 필요하므로, 노드 배치 응답이 적재된 노드의 `(space, node_type)` 를 돌려주도록 보강하거나 로더가 입력으로부터 맵을 유지한다.
- 청크/임베딩: **신규 배치 write 도구**가 필요하다 (현재 `pack_ingest` 에 청크 배치 인자 없음). 예: `pack_ingest_chunks(pack_id, chunks=[...], batch_size=256)` 또는 `pack_ingest` 에 `chunks=` 인자 추가. 신규/확장 도구는 `WRITE_TOOLS` 에 등록되어야 `_write_lock()` 직렬화를 받는다.

**MCP 의 산발적 쓰기와의 공존**
- 적재 중에도 일반 사용자/에이전트가 가끔 write 도구를 호출할 수 있다. `_write_lock()` 는 `LOCK_EX` 이므로 한 번에 하나의 write 만 진행된다.
- 로더가 **거대한 단일 호출로 락을 장시간 점유하면** 그동안 다른 쓰기가 모두 블록된다. 따라서 **배치 단위로 락 점유 시간을 제한**한다: 한 호출당 한 배치(예: 256)만 처리하고 락을 즉시 해제해, 배치 사이에 MCP 의 산발 쓰기가 끼어들 틈을 준다.
- **읽기는 락을 잡지 않으므로** 적재 내내 쿼리는 무중단으로 동작한다.

---

## 5. 단계별 전환 계획

1. **배치 write 도구 추가**
   - 청크 배치 ingest 도구 신설(또는 `pack_ingest` 에 `chunks=` 확장). `WRITE_TOOLS` 등록. 배치당 락 점유 + 즉시 해제.
   - 노드 배치 응답에 `(space, node_type)` 포함하여 로더 `id_map` 재구성 지원.
2. **로더에 MCP 클라이언트 모드 추가**
   - `--via-mcp`(가칭) 플래그 도입. 켜지면 직접 스토어 생성 대신 MCP HTTP 도구 호출.
   - 기존 직접 모드는 **기본값으로 보존** (플래그 미지정 시 현행과 동일, `chroma.lock(LOCK_EX)` 유지).
   - MCP 모드에서는 `chroma.lock` 획득을 건너뛰고 Bearer 토큰을 로드.
3. **검증** (§7)
   - 소규모 팩으로 MCP 가동 중 동시 적재, 데이터 정합성, 처리량 측정.
4. **기본 전환**
   - 검증 통과 후 `--via-mcp` 를 기본 동작으로 승격. 직접 모드는 `--direct` 등으로 잔존(롤백/오프라인 대량 재적재용).

---

## 6. 장단점

**장점**
- 신규 서버 프로세스(chroma 서버 모드)나 전면 재적재가 불필요 — **가장 가벼운 경로**.
- "적재 시 MCP 중지" 운영 제약 제거, 읽기 무중단.
- chroma 다중 프로세스 제약을 구조적으로 회피 (쓰기 프로세스가 MCP 1개).

**단점**
- 건별/배치 모두 HTTP 왕복 + `_write_lock()` 직렬화 오버헤드 → 오프라인 직접 적재보다 느릴 수 있음.
- 청크 배치 API 신설 등 MCP 도구 추가 작업 필요.
- Bearer 토큰 인증/배포를 로더가 다뤄야 함.
- 적재가 MCP 쓰기 락을 공유하므로, 락 점유 정책(배치 단위)을 잘못 잡으면 일반 쓰기 지연 가능.

---

## 7. 검증 방법

- **동시 실행:** MCP 서버 가동 상태에서 로더(`--via-mcp`)를 실행한다. 적재 중 별도 클라이언트로 `ontology_query` / `opencrab_search_nodes` 등 **읽기 쿼리가 무중단**인지 확인.
- **쓰기 공존:** 적재 중 산발적 write 도구(예: `ontology_add_node`)를 호출해 배치 사이에 정상 처리되는지(과도한 블록 없음) 확인.
- **데이터 정합성:** 동일 팩을 (a) 직접 모드, (b) MCP 모드로 각각 적재 후 노드/엣지/청크 수 및 샘플 내용이 일치하는지 비교. `id_map` 기반 엣지 연결이 누락 없이 재현되는지 확인.
- **처리량:** 배치 크기별 적재 시간 측정, 직접 모드 대비 회귀 폭 기록.
- **락 안전:** MCP 모드 적재 중 `chroma.lock(LOCK_EX)` 를 잡지 않음을 확인(로더가 chroma 미접근).

---

## 8. 롤백

- `--via-mcp` 를 끄고(또는 `--direct`) **직접 모드로 즉시 복귀**. 직접 모드는 단계 2~4 내내 보존되므로 코드 변경 없이 운영 절차만 되돌리면 된다.
- 신규 청크 배치 도구는 추가 전용(additive)이라 기존 도구/직접 적재 경로에 영향 없음.
- 직접 모드 복귀 시 기존 "MCP 중지 → 적재 → 재시작" 절차(`chroma.lock(LOCK_EX)`)가 그대로 동작.
