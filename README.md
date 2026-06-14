# LocalCrab

LocalCrab은 **로컬에서 실행하는 온톨로지 지식 서비스**입니다. 문서·데이터를 9-space MetaOntology 그래프로 적재하고, 벡터·BM25·그래프를 결합한 하이브리드 검색을 MCP 인터페이스로 제공합니다. Docker 없이 SQLite + 로컬 Chroma만으로 단일 머신에서 동작합니다.

[AlexAI-MCP/OpenCrab](https://github.com/AlexAI-MCP/OpenCrab)을 기반으로 한 로컬 배포판 fork입니다. 파이썬 패키지명·엔트리포인트는 upstream 머지 충돌을 줄이기 위해 `opencrab`을 유지합니다.

호스팅 SaaS인 **[OpenCrab](https://opencrab.sh)**은 별도 서비스입니다. LocalCrab과의 관계는 [관계 문서](./docs/localcrab-opencrab-relationship.md)를 참고하세요.

---

## 핵심 기능

- **로컬 우선**: Docker 불필요 — SQLite 그래프·문서 스토어 + 로컬 Chroma 벡터 스토어.
- **9-space MetaOntology 그래프**: 문법 검증 기반 노드·엣지 적재.
- **하이브리드 검색**: 벡터(semantic) + BM25(키워드) + 그래프 이웃 탐색을 RRF로 통합.
- **한국어 검색 품질**: OpenAI 호환 임베딩 서버(LM Studio 등) + 로컬 GGUF 폴백으로 KURE-v1 등 한국어 특화 모델 지원.
- **MCP 서버**: Claude Code·IDE·원격 클라이언트에 stdio 또는 streamableHTTP로 연결.
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
# STORAGE_MODE=local (기본) — SQLite + 로컬 Chroma
```

**로컬 모드 스토어 구성:**

| 역할 | 백엔드 | 파일 (`LOCAL_DATA_DIR` 기준) |
|------|--------|------------------------------|
| 그래프 | `LocalGraphStore` (SQLite BFS) | `graph.db` |
| 문서 | `LocalSQLDocStore` (SQLite) | `doc_store.db` |
| 벡터 | ChromaStore (PersistentClient) | `chroma/` |
| SQL | SQLStore (SQLite) | `opencrab.db` |

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

**원격 접근 (supergateway streamableHTTP, Tailscale 등):**

```bash
npx -y supergateway \
  --outputTransport streamableHttp \
  --port 8765 \
  --stdio "opencrab serve"
```

```json
{
  "mcpServers": {
    "localcrab": {
      "type": "http",
      "url": "http://<host>:8765/mcp"
    }
  }
}
```

---

## CLI 명령어

| 명령어 | 설명 |
|--------|------|
| `opencrab init` | `.env` 생성 (기본 설정 템플릿) |
| `opencrab serve` | MCP 서버 시작 (stdio) |
| `opencrab status` | 모든 스토어 연결 상태 확인 |
| `opencrab ingest <path>` | 파일을 벡터·문서 스토어에 인제스트 (`--recursive`, `--extension`, `--pack-id`) |
| `opencrab extract <path>` | LLM으로 노드·엣지 추출 후 그래프에 적재 (`--dry-run`, `--api-key`) |
| `opencrab query "<질문>"` | 하이브리드 검색 (`--spaces`, `--limit`, `--pack-id`, `--json-output`) |
| `opencrab manifest` | MetaOntology 전체 문법 출력 (`--json-output`) |
| `opencrab ocr <path>` | 이미지/문서 OCR (easyocr/tesseract/metadata 백엔드) |
| `opencrab image-context <path>` | 이미지 CLIP 스타일 증거 컨텍스트 빌드 |
| `opencrab export-neo4j-pack` | 그래프 스냅샷을 OpenCrab Pack v1 JSONL로 내보내기 |
| `opencrab assemble-pack-v1 <dir>` | 스테이징 디렉토리에서 Pack v1 ZIP 조립 |
| `opencrab packs list` | 적재된 팩 목록 |
| `opencrab packs show <pack_id>` | 팩 매니페스트 상세 |
| `opencrab packs backfill-pack-id` | 노드·엣지에 `pack_id` 역보충 |
| `opencrab packs reindex-bm25` | BM25 캐시 강제 재구성 |

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

> ReBAC/identity/promotion/billing 등 툴은 코드에 있으나 현재 MCP 미노출 상태입니다. `opencrab/mcp/tools.py`에서 해당 툴을 주석 해제하면 복원됩니다.

---

## 임베딩 백엔드

두 가지 임베딩 백엔드를 지원합니다.

**`local` (기본)**: ChromaDB 기본 EF, all-MiniLM-L6-v2 ONNX, 384d. 설정 없이 바로 동작하지만 한국어 검색 품질이 낮습니다.

**`openai` (권장)**: OpenAI 호환 임베딩 서버(LM Studio, Ollama 등) + 로컬 GGUF 폴백 자동 전환. KURE-v1 같은 한국어 특화 모델(1024d)을 쓰면 검색 품질이 크게 향상됩니다.

| 모델 | top-1 (5건) | MRR | 정답−무관 마진 | 건당 속도 |
|------|-------------|-----|----------------|-----------|
| minilm (기본, 384d ONNX) | 0/5 | 0.285 | −0.086 (무관 문서가 더 가까움) | ~0.25s 로컬 |
| KURE-v1 LM Studio (주력, 1024d) | **5/5** | **1.000** | **+0.447** | ~0.06s GPU |
| KURE-v1 로컬 GGUF (폴백, 1024d) | **5/5** | **1.000** | **+0.446** | ~1.07s CPU |

벡터 일치도(LM Studio ↔ 로컬 GGUF): cosine 평균 0.999853 — 폴백 전환 시에도 같은 컬렉션 그대로 사용.

### 설정

| 환경변수 | 기본값 | 설명 |
|----------|--------|------|
| `EMBEDDING_BACKEND` | `local` | `local` = minilm, `openai` = OpenAI 호환 서버 |
| `LMSTUDIO_API_BASE` | `http://localhost:1234/v1` | OpenAI 호환 서버 주소 |
| `LMSTUDIO_EMBED_MODEL` | `text-embedding-kure-v1` | 서버에 로드된 모델 id |
| `EMBED_DIM` | `1024` | 임베딩 차원 (모델에 맞게 설정) |
| `LOCAL_GGUF_PATH` | _(자동 다운로드)_ | 로컬 폴백 GGUF 경로 |
| `EMBED_COLLECTION` | `opencrab_vectors_kure` | openai 백엔드 전용 Chroma 컬렉션명 |
| `LMSTUDIO_TIMEOUT` | `8.0` | 서버 응답 타임아웃(초) |

```bash
export EMBEDDING_BACKEND=openai
export LMSTUDIO_API_BASE=http://<lmstudio-host>:1234/v1
export LMSTUDIO_EMBED_MODEL=text-embedding-kure-v1
opencrab serve
```

**롤백**: `EMBEDDING_BACKEND=local` 또는 미설정 → 기존 minilm 컬렉션으로 즉시 복귀.

### 초기 적재 (backfill)

`EMBEDDING_BACKEND=openai`로 전환 시, 기존 노드를 새 컬렉션으로 재임베딩합니다.

```bash
export EMBEDDING_BACKEND=openai
python backfill_kure.py
```

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
| 그래프 | Neo4j (`NEO4J_URI`) |
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

---

## 프로젝트 구조

```text
opencrab/
  grammar/        MetaOntology 문법, 검증기, 용어집
  schemas/        YAML 타입 스키마, 스키마 팩, 액션 스키마
  ontology/       빌더, 쿼리, identity, 정규화, 승인, ReBAC
  execution/      워크플로·승인 런타임
  stores/         Neo4j, Chroma, Mongo, SQL, LocalGraphStore, LocalSQLDocStore
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
