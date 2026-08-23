# LocalCrab

LocalCrab은 **로컬에서 실행하는 온톨로지 지식 서비스**입니다. 문서·데이터를 9-space MetaOntology 그래프로 적재하고, 벡터·BM25·그래프를 결합한 하이브리드 검색을 MCP 인터페이스로 제공합니다. Docker 없이 SQLite(sqlite-vec 벡터 백엔드 포함)만으로 단일 머신에서 동작합니다.

[AlexAI-MCP/OpenCrab](https://github.com/AlexAI-MCP/OpenCrab)을 기반으로 한 로컬 배포판 fork입니다. 파이썬 패키지명·엔트리포인트는 upstream 머지 충돌을 줄이기 위해 `opencrab`을 유지합니다.

호스팅 SaaS인 **[OpenCrab](https://opencrab.sh)**은 별도 서비스입니다. LocalCrab과의 관계는 [관계 문서](./docs/localcrab-opencrab-relationship.md)를 참고하세요.

---

## 핵심 기능

- **로컬 우선**: Docker 불필요 — SQLite 그래프·문서·벡터(sqlite-vec) 스토어. 벡터 백엔드는 Chroma로도 전환 가능([벡터 스토어 백엔드](#벡터-스토어-백엔드-vector_backend) 참고).
- **9-space MetaOntology 그래프**: 문법 검증 기반 노드·엣지 적재.
- **하이브리드 검색**: 벡터(semantic) + BM25(키워드) + 그래프 이웃 탐색을 RRF로 통합.
- **한국어 검색 품질**: OpenAI 호환 임베딩 서버(LM Studio 등) + 로컬 GGUF 폴백으로 KURE-v1 등 한국어 특화 모델 지원.
- **MCP 서버**: Claude Code·IDE·원격 클라이언트에 stdio 또는 직접 Streamable HTTP(`serve --transport http`)로 연결.
- **팩 내보내기** (선택): 구축한 그래프를 OpenCrab Pack v1 ZIP으로 내보내기 가능.

---

## 빠른 시작

### 1. 설치

```bash
pip install -e ".[dev]"
# Python 3.11 이상 필요
```

### 2. 초기화

```bash
opencrab init
# 현재 디렉토리에 .env 생성 — LOCAL_DATA_DIR 등 기본 설정 포함
```

### 3. 실행

```bash
opencrab serve
# STORAGE_MODE=local (기본) — SQLite(그래프·문서·벡터)
```

**로컬 모드 스토어 구성:**

| 역할 | 백엔드 | 파일 (`LOCAL_DATA_DIR` 기준) |
|------|--------|------------------------------|
| 그래프 | `LocalGraphStore` (SQLite BFS) | `graph.db` |
| 문서 | `LocalSQLDocStore` (SQLite) | `doc_store.db` |
| 벡터 | `SqliteVecStore` (기본) / `ChromaStore` (옵션) | `vectors.db` / `chroma/` |
| SQL | SQLStore (SQLite) | `opencrab.db` |

> 벡터 백엔드 기본값은 조건부입니다(`EMBEDDING_BACKEND`·`STORAGE_MODE`에 따라 결정). 상세: [벡터 스토어 백엔드 섹션](#벡터-스토어-백엔드-vector_backend), [벡터 백엔드 매트릭스](./docs/vector-backends.md).

`STORAGE_MODE=kuzu`로 실행하면 그래프만 `KuzuGraphStore`(ladybug>=0.18, `graph.kuzu`)로
바뀌고 문서·벡터·SQL 스토어는 local 모드와 동일합니다. 설치: `pip install ".[kuzu]"`.

`STORAGE_MODE=pg`로 실행하면 4스토어(graph/doc/sql/vector) 전부가 PostgreSQL 한
서버(`POSTGRES_URL`)로 통합됩니다 — `PGGraphStore`/`PgDocStore`/`SQLStore(PG)`/
`PgVectorStore`(pgvector HNSW), SQLAlchemy 공유 엔진·MVCC 다중 라이터. 설치:
`pip install ".[pg]"`. 상세: [벡터 스토어 백엔드 섹션](#벡터-스토어-백엔드-vector_backend).

> **운영 권장**: 기본은 `STORAGE_MODE=local` — graph/doc/sql/vector(sqlite-vec)를
> 전부 SQLite 한 규율로 통일해 백업 디렉터리 1개·정합성 관리 대상 1개로 운영합니다.
> 실시간 동시 write(MCP 서빙 중 백그라운드 로더)가 확정 요구이거나 벡터가 수백만
> 스케일로 커지면 `STORAGE_MODE=pg`(PostgreSQL 단일 통합, 4스토어 전부 PG·MVCC 다중
> 라이터, `pip install ".[pg]"` — [pgvector-migration-plan.md](./docs/pgvector-migration-plan.md)
> (B) 경로)로 이행하세요. 기존 SQLite → PG 데이터 이관은
> `scripts/migrate_sqlite_to_pg.py`(1:1 복사, 재임베딩 불필요) 참고.
> `docker` 모드(Neo4j+MongoDB+PostgreSQL+Chroma 4종 혼합)는 SaaS 규모가 아니면
> 비권장입니다 — Neo4j/Mongo 각각의 이점이 4종 스토어를 따로 백업·버전관리·정합성
> 관리하는 비용을 상회하지 못합니다.

아키텍처 상세는 [ARCHITECTURE.md](./docs/ARCHITECTURE.md) 참고.

### 4. 적재 & 질의

```bash
# 파일 인제스트 (벡터 + 문서 스토어)
opencrab ingest ./docs --recursive --extension .md,.txt,.pdf

# 하이브리드 검색
opencrab query "시스템 성능 지표 및 오류율"

# 현재 적재된 그래프 상태 확인
opencrab status

# MetaOntology 전체 문법 출력
opencrab manifest
```

### 5. MCP 서버 연결

**stdio (Claude Code 등):**

```bash
claude mcp add localcrab -- opencrab serve
```

또는 설정 파일에 직접 추가:

```json
{
  "mcpServers": {
    "localcrab": {
      "command": "opencrab",
      "args": ["serve"]
    }
  }
}
```

**원격 접근 (직접 Streamable HTTP, Tailscale·cloudflared 등):**

`serve --transport http`가 `/mcp` 엔드포인트를 직접 노출한다(supergateway 불필요).

**인증은 사용자별 토큰뿐이다(#145).** 무인증 모드와 공유 비밀(`--auth-token-file`, `LOCALCRAB_MCP_TOKEN`, `OPENCRAB_API_KEY`)은 삭제됐다 — 그 환경변수가 남아 있으면 **기동을 거부**한다(효력 없는 변수를 보고 보호받는다고 믿는 상태가 가장 위험하다).

```bash
# 1) 로컬 사용자 + 첫 토큰 생성 (1회)
opencrab init

# 2) 원격 클라이언트마다 사용자와 토큰 발급
opencrab user add "my-laptop"
opencrab token issue <user_id>          # 평문은 이때 한 번만 출력된다

# 3) 기동 (헤더 인증)
opencrab serve --transport http --host 127.0.0.1 --port <port>

# 4) 헤더를 못 보내는 클라이언트가 있을 때만
opencrab serve --transport http --host 127.0.0.1 --port <port> --allow-query-token
```

```json
{
  "mcpServers": {
    "localcrab": {
      "type": "http",
      "url": "http://<host>:<port>/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

> **`--allow-query-token` 은 기본 꺼짐이다.** 켜면 `"url": "http://<host>:<port>/mcp?token=<token>"` 형태가 통하지만, URL 자격증명은 액세스 로그·프록시 로그·브라우저 히스토리·Referer 헤더에 남는다. 켠 배포는 토큰을 더 자주 회전하고 클라이언트마다 별도 토큰을 발급할 것.
>
> **어느 클라이언트가 어느 방식을 지원하는지는 [`docs/mcp-client-auth.md`](docs/mcp-client-auth.md) 를 본다.** 특히 **claude.ai 웹은 커스텀 헤더를 설정할 수 없어 쿼리 파라미터가 유일한 수단**이다. 인증 메커니즘을 제거하거나 기본값을 바꾸기 전에 그 표에서 사용 중인 클라이언트가 없는지 먼저 확인할 것.
>
> 헤더와 쿼리 토큰이 함께 오면 **헤더가 결정한다.** 헤더가 있는데 무효면 쿼리로 폴백하지 않고 401 이다 — 폴백하면 쓰레기 헤더를 붙여 `--allow-query-token` 제한을 우회할 수 있다.
>
> 브라우저에 직접 노출하는 배포는 `OPENCRAB_CORS_ORIGINS` 로 **Origin 허용 목록을 명시**한다. 비워 두면 교차 출처 접근이 차단된다(wildcard 로 열리지 않는다).

> **uvicorn 단일 워커**로 실행된다(serve가 강제). 원래 chroma PersistentClient 단일 프로세스 제약 때문이었고, `VECTOR_BACKEND=sqlite-vec`(SQLite WAL)에서는 하드 제약이 아니나 프로세스별 in-memory BM25/임베딩 유지를 위해 1로 둔다. `/mcp` 의 POST·GET·DELETE 모두 인증을 요구한다(`/healthz` 는 예외).

---

## CLI 명령어

| 명령어 | 설명 |
|--------|------|
| `opencrab init` | `.env` 생성 (기본 설정 템플릿) |
| `opencrab serve` | MCP 서버 시작 (stdio 기본; `--transport http` 로 Streamable HTTP). 인증은 사용자별 토큰 필수 |
| `opencrab user add\|list\|disable\|enable` | 사용자 관리 |
| `opencrab token issue\|list\|revoke` | 사용자별 토큰 관리 (평문은 발급 시 1회만 출력) |
| `opencrab status` | 모든 스토어 연결 상태 확인 |
| `opencrab ingest <path>` | 파일을 벡터·문서 스토어에 인제스트 (`--recursive`, `--extension`, `--pack-id`) |
| `opencrab extract <path>` | LLM으로 노드·엣지 추출 후 그래프에 적재 (`--dry-run`, `--api-key`) |
| `opencrab query "<질문>"` | 하이브리드 검색 (`--spaces`, `--limit`, `--pack-id`, `--json-output`) |
| `opencrab manifest` | MetaOntology 전체 문법 출력 (`--json-output`) |
| `opencrab ocr <path>` | 이미지/문서 OCR (easyocr/tesseract/metadata 백엔드)[^media] |
| `opencrab image-context <path>` | 이미지 CLIP 스타일 증거 컨텍스트 빌드[^media] |
| `opencrab export-neo4j-pack` | 그래프 스냅샷을 OpenCrab Pack v1 JSONL로 내보내기 |
| `opencrab assemble-pack-v1 <dir>` | 스테이징 디렉토리에서 Pack v1 ZIP 조립 |
| `opencrab packs list` | 적재된 팩 목록 |
| `opencrab packs show <pack_id>` | 팩 매니페스트 상세 |
| `opencrab packs backfill-pack-id` | 노드·엣지에 `pack_id` 역보충 |
| `opencrab packs reindex-bm25` | BM25 캐시 강제 재구성 |
| `opencrab packs repair-registry` | 생성이 끝나지 않은 팩 등록부 행을 판정·해소 (`--older-than`, `--promote`, `--apply`)[^repair] |
| `opencrab packs repair-anchors` | `ready` 팩이 잃어버린 graph 앵커를 다시 만듦 (`--pack-id`, `--apply`)[^anchors] |

[^repair]: 등록부 행과 팩 콘텐츠는 한 트랜잭션이 아니라, 그 사이에서 프로세스가 죽으면 `ready` 에 도달하지 못한 행이 남습니다. 이 명령은 graph 앵커를 실제로 조회해 판정합니다: 앵커가 있으면 `ready` 로 승격하고, 앵커가 없음이 확인되면 `partial` 로 강등하며, 스토어를 조회할 수 없으면 아무것도 하지 않습니다. **어떤 경우에도 등록부 행을 지우지 않습니다** — 콘텐츠가 실제로 안착한 팩의 행을 지우면 `assert_registry_covers_graph` 가 다음 기동을 거부하기 때문입니다. `--apply` 없이는 계획만 출력합니다. `partial` 행은 자동으로 손대지 않고, 운영자가 `--promote <pack_id> --apply` 로 지목해야 승격하며 이때도 graph 앵커가 확인될 때만 승격합니다.

[^anchors]: `repair-registry` 의 형제 명령이되 대상이 반대입니다. 저쪽이 `creating`·`partial` 행을 다루는 반면 이쪽은 이미 `ready` 인 팩에서 `dataset:{pack_id}` 앵커만 사라진 경우를 고칩니다(옛 마이그레이션 팩, 수동 삭제, 앵커가 애초에 없던 덤프). 등록부의 title·description 으로 앵커를 다시 만들며, fork 로 생긴 팩이면 그 출처 표식도 함께 되살립니다. 멱등이라 앵커가 이미 있으면 아무것도 하지 않고, **등록부 행의 상태는 어느 경우에도 바꾸지 않습니다.** `--apply` 없이는 계획만 출력하고, `--pack-id` 로 한 팩만 지목할 수 있습니다.

[^media]: easyocr/torch는 기본 pip extra에 포함되지 않습니다. 별도 설치 필요: `pip install -r requirements/localcrab-media.txt`.

---

## MCP 툴 (16개)

| 그룹 | 툴 | 설명 |
|------|----|------|
| **문법·노드** | `ontology_manifest` | MetaOntology OS 전체 문법 반환 |
| | `ontology_add_node` | 문법 검증 후 노드 추가/업데이트 |
| | `ontology_add_edge` | 문법 검증 후 방향 엣지 추가 |
| **조회** | `ontology_query` | 벡터+BM25+그래프 하이브리드 검색 (RRF 재랭킹, pack 필터, ReBAC 필터) |
| | `ontology_get_node` | node_id로 단일 노드 조회 |
| | `ontology_list_nodes` | 노드 목록 (space·pack_id 필터) |
| | `ontology_list_edges` | 엣지 목록 (pack_id 필터) |
| **분석** | `ontology_impact` | I1–I7 임팩트 분석 |
| | `ontology_lever_simulate` | 레버 조정 시 하위 outcome 변화 시뮬레이션 |
| **콘텐츠 팩** | `content_pack_list` | 적재된 팩 목록 (노드 수·타이틀) |
| | `pack_create` | 팩 신규 생성 + 노드·엣지·텍스트 인제스트 |
| | `pack_ingest` | 기존 팩에 노드·엣지·텍스트 추가 |
| **스키마 팩** | `schema_pack_list` | 사용 가능한 스키마 팩 목록 (설치 여부) |
| | `schema_pack_install` | 도메인 스키마 팩 설치 |
| | `schema_pack_uninstall` | 스키마 팩 제거 |
| **하니스** | `harness_promotion_apply` | CrabHarness PromotionPackage 적용 (`dry_run` 지원) |

> ReBAC/identity/promotion_promote/billing_get_usage 등 MCP 툴은 실사용 이력이 없어 죽은 코드로 삭제됐습니다(git history에 보존, 필요 시 복원). `opencrab/mcp/tools.py`는 더 이상 존재하지 않으며, 핸들러는 `opencrab/mcp/tools/`(graph.py/query.py/pack.py/schema.py/harness.py) 아래로 물리 분할됐습니다. 참고로 과금 자체(`opencrab.billing.hooks.BillingHooks`, `billing_events` 테이블)는 별개로 계속 살아 있고 `ontology_add_node`/`ontology_add_edge`/`pack_create`/`pack_ingest`/`harness_promotion_apply`에서 배선되어 있습니다 — 삭제된 것은 `billing_get_usage` 같은 조회용 MCP 툴 노출뿐입니다.

> **사용자별 노출 (#150).** 위 16개는 로컬 principal(stdio/CLI) 기준입니다. 원격(토큰 인증) principal은 `schema_pack_install` / `schema_pack_uninstall` / `harness_promotion_apply`(관리 등급, 호스트 파일시스템 또는 팩 경계 밖 쓰기) 3개가 `tools/list`에서 빠지고 `tools/call`로 직접 호출해도 거부됩니다(13개만 사용 가능). 등급은 `users.role` 컬럼이 아니라 `Principal.is_local`에서 유도합니다 — 로컬 사용자는 어차피 그 파일들을 직접 편집할 수 있는 주체라 나눌 실익이 있는 경계가 로컬 대 원격이기 때문입니다. 원격 관리자가 둘 이상 필요해지면 그때 역할 컬럼을 도입합니다. 자세한 내용은 `opencrab/mcp/tools/_registry.py`의 `AccessTier`/`allowed_access_tiers`를 참고하세요.

---

## 임베딩 백엔드

두 가지 임베딩 백엔드를 지원합니다.

**`openai` (기본)**: OpenAI 호환 임베딩 서버(LM Studio, Ollama 등)를 primary로, 로컬 GGUF를 fallback으로 쓰는 `ResilientEmbeddingFunction` 구조입니다. KURE-v1(한국어 특화, 1024d)이 기본 모델입니다. GGUF 폴백은 KURE-v1-Q8_0(약 635MB, 품질 우선)을 자동 다운로드하며, 외부 서버 없이도 완전 로컬로 동작할 수 있습니다(`pip install "opencrab[gguf]"`로 `llama-cpp-python` 설치 필요). 저사양 환경은 `LOCAL_GGUF_PATH`로 Q4_K_M(438MB) 등 다른 양자화를 지정할 수 있습니다.

**`local` (롤백 옵션)**: ChromaDB 기본 EF, all-MiniLM-L6-v2 ONNX, 384d. 설정 없이 바로 동작하지만 한국어 검색 품질이 낮습니다.

| 모델 | top-1 (5건) | MRR | 정답−무관 마진 | 건당 속도 |
|------|-------------|-----|----------------|-----------|
| minilm (롤백용, 384d ONNX) | 0/5 | 0.285 | −0.086 (무관 문서가 더 가까움) | ~0.25s 로컬 |
| KURE-v1 LM Studio (기본, 1024d) | **5/5** | **1.000** | **+0.447** | ~0.06s GPU |
| KURE-v1 로컬 GGUF (폴백, 1024d) | **5/5** | **1.000** | **+0.446** | ~1.07s CPU |

벡터 일치도(LM Studio ↔ 로컬 GGUF): cosine 평균 0.999853 — 폴백 전환 시에도 같은 컬렉션 그대로 사용.

> **참고**: `openai`는 *모델*이 아니라 *백엔드(전송 방식)* 입니다. OpenAI 호환 `/v1/embeddings`
> API면 무엇이든 쓸 수 있습니다 — 실제 OpenAI 클라우드 모델(`text-embedding-3-small`/`-large`)도,
> 자체호스팅 서버(LM Studio·Ollama·vLLM·HF TEI)에 올린 모델도 가능. 모델은 `OPENAI_EMBED_MODEL`,
> 차원은 `EMBED_DIM`으로 맞춥니다. **한국어 검색에는 KURE-v1을 추천**합니다(번들 GGUF 폴백도 KURE라
> turnkey). 단 **한 컬렉션 = 한 모델** 원칙으로, 모델을 바꾸면 새 `EMBED_COLLECTION` + 전량 재색인이
> 필요합니다(서로 다른 모델 벡터를 한 컬렉션에 섞지 말 것).

**경량 대안 (CPU 부담 시)**: KURE-v1은 BGE-M3 기반(약 560M, 1024d)이라 CPU만으로는 무겁습니다.
CPU 자원이 부족하면 한국어 경량 임베딩 [`BM-K/KoSimCSE-roberta`](https://huggingface.co/BM-K/KoSimCSE-roberta)
(RoBERTa-base, 약 110M, 768d)를 추천합니다. KURE보다 가볍고 빠르지만 한국어 전용이며 검색 품질은
다소 낮을 수 있습니다. OpenAI 호환 서버(예: HF Text-Embeddings-Inference)에 KoSimCSE를 서빙하고
`OPENAI_EMBED_MODEL`, `EMBED_DIM=768`, 별도 `EMBED_COLLECTION`만 지정하면 코드 수정 없이 사용
가능합니다(전량 재색인 필요). 단 로컬 GGUF 폴백은 GGUF 빌드가 있어야 하므로 KoSimCSE에는 기본
적용되지 않습니다(원격 primary만 사용).

### 설정

| 환경변수 | 기본값 | 설명 |
|----------|--------|------|
| `EMBEDDING_BACKEND` | `openai` | `openai` = OpenAI 호환 서버(+GGUF 폴백), `local` = minilm(롤백) |
| `OPENAI_API_BASE` | `http://localhost:1234/v1` | OpenAI 호환 서버 주소. 콤마 구분으로 여러 URL 지정 시 순서대로 시도하는 체인(첫 서버 장애 → 다음 서버 → 전부 장애 시 GGUF 폴백) |
| `OPENAI_EMBED_MODEL` | `text-embedding-kure-v1` | 서버에 로드된 모델 id |
| `OPENAI_API_KEY` | _(없음)_ | 임베딩 서버가 인증을 요구할 때의 Bearer 토큰 (아웃바운드 자격증명) |
| `EMBED_DIM` | `1024` | 임베딩 차원 (모델에 맞게 설정) |
| `LOCAL_GGUF_PATH` | _(자동 다운로드)_ | 로컬 폴백 GGUF 경로 |
| `EMBED_COLLECTION` | `opencrab_vectors_kure` | openai 백엔드 전용 Chroma 컬렉션명 |
| `OPENAI_TIMEOUT` | `8.0` | 서버 응답 타임아웃(초) |

```bash
export EMBEDDING_BACKEND=openai
export OPENAI_API_BASE=http://<server-host>:1234/v1
export OPENAI_EMBED_MODEL=text-embedding-kure-v1
opencrab serve
```

**다중 원격 엔드포인트(장애 대비)**: `OPENAI_API_BASE`에 콤마로 여러 URL을 나열하면
순서대로 시도된다. 첫 서버가 죽어도 GGUF(CPU) 폴백으로 내려가기 전에 다음 서버를
우선 시도한다. 각 엔드포인트는 독립적으로 헬스 TTL을 추적하므로, 죽어 있는 서버
하나가 매 요청마다 지연을 만들지 않는다. 모든 엔드포인트가 동일 모델(KURE-v1)을
서빙한다고 가정한다(컬렉션 재사용 보장).

```bash
export OPENAI_API_BASE="http://embed-host-1:1234/v1,http://embed-host-2:1234/v1"
```

**롤백**: `EMBEDDING_BACKEND=local` → minilm 컬렉션으로 즉시 복귀(단, `VECTOR_BACKEND=sqlite-vec`와는 조합 불가 — 아래 [벡터 스토어 백엔드](#벡터-스토어-백엔드-vector_backend) 참고).

### 초기 적재 (backfill)

기존에 `local`(minilm) 컬렉션으로 적재해둔 노드가 있고 `openai`(KURE)로 전환하는 경우, 기존 노드를 새 컬렉션으로 재임베딩해야 합니다.

```bash
export EMBEDDING_BACKEND=openai
```

> `backfill_kure.py`는 이 레포에 **포함되어 있지 않은 외부 운영 스크립트**입니다(`~/opencrab-dump` 쪽에서 관리, vector/doc upsert 전용). 위 환경변수를 설정한 뒤 해당 운영 스크립트를 실행해 재임베딩하세요. 상세: [docs/ingestion-via-mcp-plan.md](./docs/ingestion-via-mcp-plan.md).

## 벡터 스토어 백엔드 (`VECTOR_BACKEND`)

임베딩 백엔드(`EMBEDDING_BACKEND`)와 **독립된 축**으로, 벡터를 어디에 저장·검색할지 고릅니다.
`VECTOR_BACKEND`를 명시하지 않으면 아래 규칙으로 조건부 결정됩니다.

- `STORAGE_MODE=local`(또는 `kuzu`) + `EMBEDDING_BACKEND=openai`(기본) → **`sqlite-vec`**
- `STORAGE_MODE=pg` → **`pgvector`** (4스토어 PG 통합의 벡터 축)
- `STORAGE_MODE=docker` 이거나 `EMBEDDING_BACKEND=local`(minilm) → **`chroma`**
- `VECTOR_BACKEND`를 명시하면 항상 그 값이 우선합니다.
- 예외: `VECTOR_BACKEND=sqlite-vec`를 명시했는데 `EMBEDDING_BACKEND=local`이면 기동 시 `ValueError`(minilm 384d는 sqlite-vec에서 미지원).

모드×옵션 조합 전체 매트릭스와 백엔드별 장단점 상세는 [벡터 스토어 백엔드 매트릭스](./docs/vector-backends.md) 참고.

**`sqlite-vec` (로컬 모드 기본)**: sqlite-vec(vec0) — 벡터를 graph/doc/sql 과 **같은 SQLite WAL 규율**에 편입해
Chroma의 "다중 프로세스 동시 쓰기 불가"(자작 flock 층)를 제거합니다. 앱이 KURE EF로 직접 임베딩 후
`vec0` 테이블에 INSERT하므로 `EMBEDDING_BACKEND=openai`(KURE 1024d)와 함께 씁니다. 벡터 DB는
`LOCAL_DATA_DIR/vectors.db`. 설계·트레이드오프: `docs/pgvector-migration-plan.md` (A) 경로.

> 특성: pack-scoped 검색은 매우 빠르나(수 ms), 전역(pack 미지정) 검색은 기본 브루트포스라 대규모에서 느립니다.
> 전역 고속화는 `VECTOR_ANN=binary`(binary 2단계 양자화, 기본 off) 옵트인으로 제공 —
> 상세·마이그레이션·롤백은 [벡터 백엔드 매트릭스 §4.1](./docs/vector-backends.md) 참고. 정확도는 exact라 Chroma HNSW보다 높습니다.

**`chroma` (docker 모드 기본 / local+minilm 조합 기본)**: ChromaDB. 로컬은 PersistentClient, docker는 HttpClient. 기존 동작 100% 보존.

**`pgvector`** (`STORAGE_MODE=pg`에서 자동 선택): PostgreSQL 확장. HNSW 인덱스
(`m=16, ef_construction=64`, 쿼리 시 `hnsw.ef_search=PG_EF_SEARCH` 기본 500)로
전역 검색도 179,784건 전량 실측 p95 24.61ms — sqlite-vec의 binary 2단계 같은 별도 가속이 불필요.
`pip install ".[pg]"` 필요. `STORAGE_MODE!=pg`에서도 `VECTOR_BACKEND=pgvector`를
명시하면 벡터만 PG로 보낼 수 있습니다. 설계·실측: `docs/pgvector-migration-plan.md` (B) 경로.

| 환경변수 | 기본값 | 설명 |
|----------|--------|------|
| `VECTOR_BACKEND` | _(미설정 — 위 조건부 규칙으로 결정)_ | `chroma` \| `sqlite-vec` \| `pgvector` |
| `VECTOR_DB_FILE` | `vectors.db` | sqlite-vec 벡터 DB 파일명(`LOCAL_DATA_DIR` 하위) |
| `VECTOR_COLLECTION` | `vectors_kure` | sqlite-vec vec0 테이블명 |
| `VECTOR_ANN` | _(미설정 = off)_ | `binary` = 전역 검색 2단계 양자화 가속(sqlite-vec 전용) |
| `VECTOR_ANN_COARSE_K` | `512` | binary 2단계 coarse 후보 수(recall 튜닝) |
| `PG_EF_SEARCH` | `500` | pgvector HNSW 쿼리 세션 파라미터(recall/속도 트레이드오프, pgvector 전용) |

```bash
# 기존 Chroma 컬렉션이 있는 상태에서 sqlite-vec로 전환(KURE 벡터를 그대로 1:1 이관)
python scripts/migrate_chroma_to_sqlite_vec.py      # chroma → vectors.db
export EMBEDDING_BACKEND=openai VECTOR_BACKEND=sqlite-vec
opencrab serve
```

> **무중단 적재(sqlite-vec)**: chroma의 `chroma.lock(LOCK_EX)` 제약이 사라져 **적재 시 게이트웨이/서비스를 중단할 필요가 없다.** 벡터를 포함한 4스토어가 모두 SQLite WAL이라 로더/reingest 쓰기와 serve 읽기가 동시 진행되고, 라이터는 `write.lock`/SQLite `busy_timeout(5s)`로 직렬화된다. (chroma 백엔드에서는 기존대로 오프라인 `--fresh` 적재 시 중단 필요.)

**롤백**: `VECTOR_BACKEND=chroma` 명시 → Chroma 스택으로 즉시 복귀(비파괴, Chroma 보존).

```bash
# STORAGE_MODE=pg — 4스토어(graph/doc/sql/vector) 전부 PostgreSQL 한 서버로 통합
pip install ".[pg]"
export STORAGE_MODE=pg
export POSTGRES_URL=postgresql://opencrab:opencrab@localhost:5432/opencrab
opencrab serve

# 기존 로컬 SQLite(graph.db/doc_store.db/opencrab.db/vectors.db) → PG 1회 이관
# (재임베딩 없음 — vectors.db의 raw float 벡터를 그대로 복사)
python scripts/migrate_sqlite_to_pg.py --pg-url "$POSTGRES_URL" --dry-run
python scripts/migrate_sqlite_to_pg.py --pg-url "$POSTGRES_URL" --verify
```

---

## 기타 환경변수

| 환경변수 | 기본값 | 설명 |
|----------|--------|------|
| `ANTHROPIC_API_KEY` | _(없음)_ | `opencrab extract`의 `--api-key` 미지정 시 폴백(Claude 모델 호출용) |
| `OPENCRAB_BM25_NODE_LIMIT` | `50000` | BM25 인덱스가 doc 스토어에서 로드하는 노드 상한(인덱스 빌드 시간·메모리 제한). 상세: [ARCHITECTURE.md §6](./docs/ARCHITECTURE.md) |
| `OPENCRAB_BM25_DEBOUNCE` | `1.5` | BM25 백그라운드 재빌드 디바운스(초) — 연속 ingest를 1회 재빌드로 합침. 상세: [ARCHITECTURE.md](./docs/ARCHITECTURE.md) |
| `OPENCRAB_AUTO_PACK_MIN_SCORE` | `10.0` | 자동 팩 선택(키워드 기반 결정적 스코어링)에서 top-1 후보를 채택하는 최소 점수. 미달 시 팩 필터 없이 조회 |

---

## MetaOntology OS

### 9 Spaces

| Space | 역할 |
|-------|------|
| `subject` | 주체 — identity·agency·역할·권한을 가진 행위자 |
| `resource` | 자원 — 문서·데이터셋·도구·API·파일·프로젝트 |
| `evidence` | 증거 — 원시 관측·로그·텍스트 단위·OCR 출력·실증 기록 |
| `concept` | 개념 — 엔티티·주제·클래스·도메인 추상 |
| `claim` | 주장 — 증거에 근거한 파생 단언 |
| `community` | 커뮤니티 — 연관 개념 또는 행위자의 클러스터·요약 |
| `outcome` | 결과 — KPI·리스크·임팩트·측정 가능한 결과 |
| `lever` | 레버 — outcome·concept에 영향을 주는 조정 가능한 제어값 |
| `policy` | 정책 — 접근·민감도·승인·거버넌스 규칙 |

### 문법 확장

`opencrab/grammar/manifest.py`의 `META_EDGES`·`SPACES`·`NODE_TYPES`를 수정해 도메인별 엣지 관계와 노드 타입을 추가할 수 있습니다. 기존 공개 문법은 `opencrab manifest`로 확인하세요.

---

## Docker 모드 (선택)

`STORAGE_MODE=docker`로 외부 서비스에 연결합니다.

```bash
STORAGE_MODE=docker opencrab serve
```

| 역할 | 백엔드 |
|------|--------|
| 그래프 | Neo4j (`NEO4J_URI`, `NEO4J_DATABASE`) |
| 문서 | MongoDB (`MONGODB_URI`) |
| 벡터 | Chroma HTTP (`CHROMA_HOST:CHROMA_PORT`) |
| SQL | PostgreSQL (`POSTGRES_URL`) |

> **SQLite 버전 요구사항**: 로컬 모드는 `json_extract()` 사용으로 **SQLite 3.9.0 이상**이 필요합니다.
> `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"` 로 확인하세요.
>
> **로컬 모드 ReBAC 제약**: 그래프 권한 탐색이 Python BFS(`find_neighbors()`)로 동작합니다. 직접 및 전이적(member_of/manages → permission) 경로는 완전 지원. depth 2 초과 복잡한 다중 홉 패턴은 미지원.

### Docker → Local 모드 마이그레이션

```bash
# Dry-run: 연결 및 데이터 수량 확인 (쓰기 없음)
uv run python scripts/migrate_to_local.py --dry-run

# 실제 마이그레이션 (기존 로컬 DB 파일 자동 백업)
uv run python scripts/migrate_to_local.py
```

---

## 팩 내보내기 (선택 기능)

구축한 그래프를 **OpenCrab Pack v1 ZIP**으로 내보낼 수 있습니다.

```text
manifest.json
graph/nodes.jsonl
graph/edges.jsonl
evidence/index.jsonl
quality/report.json
neo4j/import.cypher
neo4j/opencrab_ingest.jsonl
neo4j/export_status.json
README.md
sample_queries.json
community_reports.json
```

포맷 상세: [OpenCrab Pack v1 ZIP 형식](./docs/opencrab-pack-v1.md)

`opencrab/pack/`(팩 빌드·검증·적재·내보내기 공용 라이브러리) 모듈 지도와 설계 원칙은 [팩 계약 계층](./docs/pack-contract-layer.md) 참고.

### CrabHarness

[`crabharness/`](./crabharness/)는 대규모 수집·파싱 작업을 위한 미션 기반 증거 수집 제어판입니다. 크롤 대상·범위·성공 기준을 미션으로 동결하고, 증거 번들을 검증한 뒤 PromotionPackage를 생성합니다. 상세는 [CrabHarness README](./crabharness/README.md) 참고.

---

## 개발

```bash
make dev-install   # 의존성 설치 (개발 모드)
make seed          # 샘플 온톨로지 시드 데이터 로드
make test          # 전체 테스트 실행
make status        # 스토어 연결 상태 확인
make manifest      # MetaOntology 문법 출력
make lint          # ruff 코드 검사
make format        # black + isort 포매팅
make coverage      # 커버리지 리포트
```

통합 테스트 (Neo4j·MongoDB·Chroma 도커 필요):

```bash
OPENCRAB_INTEGRATION=1 pytest tests/ -v
```

### PG 파리티 테스트

`STORAGE_MODE=pg` (PGGraphStore/PgDocStore) 골든 파리티 테스트는 별도의 로컬
PostgreSQL 인스턴스가 필요하며, `OPENCRAB_PG_TEST_URL` 미설정 시 자동으로
skip됩니다. 실제 데이터 손상을 막기 위해 DB명이 `_test`로 끝나지 않으면
tripwire 테스트가 실패합니다 — 반드시 전용 테스트 DB를 사용하세요.

```bash
docker compose up -d postgres         # pgvector/pgvector:pg16 기동
docker exec opencrab-postgres createdb -U opencrab opencrab_test  # 최초 1회
make test-pg                          # OPENCRAB_PG_TEST_URL 자동 설정 후 실행
```

CI(`.github/workflows/ci.yml`)는 동일한 구성을 postgres 서비스 컨테이너로
띄워 매 PR마다 실행합니다.

---

## 프로젝트 구조

```text
opencrab/
  grammar/        MetaOntology 문법, 검증기, 용어집
  schemas/        YAML 타입 스키마, 스키마 팩, 액션 스키마
  ontology/       빌더, 쿼리, identity, 정규화, 승인, ReBAC
  execution/      워크플로·승인 런타임
  stores/         Local(SQLite)·Kuzu(ladybug)·PG(pgvector)·docker(Neo4j/Mongo/Chroma) 스토어 + 임베딩 EF
  mcp/            MCP 서버 및 툴 레지스트리
crabharness/
  crabharness/    미션 플래너, 런타임, 검증, 프로모션 패키지 빌더
  codex_workers/  크롤러·수집기 플러그인 워커
  missions/       예제 미션
docs/             아키텍처, 팩 형식, 관계 문서
```

---

## 라이선스

MIT. [AlexAI-MCP/OpenCrab](https://github.com/AlexAI-MCP/OpenCrab) 기반 fork.
