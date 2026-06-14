<p align="center">
  <img src="logo.png" alt="LocalCrab Logo" width="260"/>
</p>

# LocalCrab

LocalCrab is a local-first ontology factory: crawl, parse, index evidence, validate against the MetaOntology OS grammar, and export as portable OpenCrab Pack v1 ZIPs.

This repository contains:
- MetaOntology OS grammar, MCP tools, and store backends
- CrabHarness evidence collection and promotion lifecycle
- Pack export contracts and schema registry

## What This Repo Is For

| Layer | Role |
| --- | --- |
| LocalCrab | Local ontology factory — crawling, parsing, evidence indexing, graph validation, ZIP pack export. |
| CrabHarness | Mission-first control plane — crawler planning, worker delegation, evidence validation, promotion packages. |
| MetaOntology OS | Canonical grammar, schemas, ReBAC, identity/canonicalization, promotion lifecycle, MCP server tools. |

Intended flow:

```text
source material or crawl target
        |
        v
CrabHarness mission planning
        |
        v
evidence collection + OCR/CLIP indexing
        |
        v
MetaOntology grammar extraction
        |
        v
local graph validation
        |
        v
OpenCrab Pack v1 ZIP
```

## Quick Start

### 1. Install

```bash
pip install -e ".[dev]"
```

### 2. Run

```bash
opencrab serve
```

LocalCrab runs locally by default. It uses SQLite and a local Chroma persistent store under `./opencrab_data`.

**Local mode store backends:**

| Role | Backend | File |
| --- | --- | --- |
| Graph | `LocalGraphStore` (SQLite BFS) | `opencrab_data/graph.db` |
| Document | `LocalSQLDocStore` (SQLite) | `opencrab_data/doc_store.db` |
| Vector | ChromaStore (local PersistentClient) | `opencrab_data/chroma/` |
| SQL | SQLStore (SQLite) | `opencrab_data/opencrab.db` |

See [Architecture](./docs/ARCHITECTURE.md) for design rationale.

## 임베딩 백엔드

두 가지 임베딩 백엔드를 지원합니다.

**기본 (`local`)**: ChromaDB 기본 EF, all-MiniLM-L6-v2 ONNX, 384d, 영어 특화.
설정 없이 동작하며 한국어 검색 품질이 낮습니다.

**권장 (`openai`)**: OpenAI 호환 임베딩 서버(LM Studio 등) + 로컬 GGUF 폴백 자동 전환.
KURE-v1(1024d) 등 한국어 특화 모델을 사용하면 검색 품질이 크게 향상됩니다.

| 모델 | top-1 | MRR | 정답-무관 마진 | 건당 속도 |
|------|-------|-----|----------------|-----------|
| minilm (기본, 384d ONNX) | 0/5 | 0.285 | −0.086 (무관↑) | 0.25s 로컬 |
| KURE-v1 LM Studio (주력, 1024d) | 5/5 | 1.000 | +0.447 | 0.06s GPU |
| KURE-v1 로컬 GGUF (폴백, 1024d) | 5/5 | 1.000 | +0.446 | 1.07s CPU |

벡터 일치도(LM Studio↔로컬 GGUF): cosine 평균 0.999853 — 폴백 호환 입증.

### 설정

환경변수:

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `EMBEDDING_BACKEND` | `local` | `local` = minilm, `openai` = OpenAI 호환 서버 |
| `LMSTUDIO_API_BASE` | `http://localhost:1234/v1` | OpenAI 호환 서버 주소 |
| `LMSTUDIO_EMBED_MODEL` | `text-embedding-kure-v1` | 서버에 로드된 모델 id |
| `LOCAL_GGUF_PATH` | _(자동 다운로드)_ | 로컬 폴백 GGUF 경로 |
| `EMBED_COLLECTION` | `opencrab_vectors_kure` | openai 백엔드 전용 Chroma 컬렉션명 |

```bash
export EMBEDDING_BACKEND=openai
export LMSTUDIO_API_BASE=http://localhost:1234/v1
export LMSTUDIO_EMBED_MODEL=text-embedding-kure-v1
opencrab serve
```

롤백: `EMBEDDING_BACKEND=local` 또는 미설정 → 기존 minilm 컬렉션 즉시 복귀.

### 초기 적재 (backfill)

`EMBEDDING_BACKEND=openai` 전환 시, 기존 노드를 새 컬렉션으로 재임베딩합니다.

```bash
export EMBEDDING_BACKEND=openai
python backfill_kure.py
```

**Docker backend:**

Set `STORAGE_MODE=docker` to connect to external Neo4j, Chroma, MongoDB, and PostgreSQL instances.

```bash
STORAGE_MODE=docker opencrab serve
```

> **SQLite version requirement:** Local mode requires **SQLite 3.9.0+** for `json_extract()`.
> Check with `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`.
>
> **`ontology_rebac_check` in local mode:** Graph-based permission traversal uses
> Python BFS via `find_neighbors()`. Direct and transitive (member_of/manages →
> permission relation) access paths are fully supported. Complex multi-hop patterns
> beyond depth 2 are not.

### 3. Verify

```bash
opencrab status
opencrab manifest
opencrab query "system performance and error rates"
```

### 4. Add as MCP server

```bash
claude mcp add opencrab -- opencrab serve
```

Or manually:

```json
{
  "mcpServers": {
    "opencrab": {
      "command": "opencrab",
      "args": ["serve"]
    }
  }
}
```

### 5. Remote MCP access via supergateway (optional)

To access LocalCrab from remote devices (e.g. over Tailscale):

```bash
npx -y supergateway \
  --outputTransport streamableHttp \
  --port 8765 \
  --stdio "opencrab serve"
```

Then connect from any MCP client:

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

## Migrating from Docker to Local Mode

```bash
# Dry-run: check connections and data counts, no writes
uv run python scripts/migrate_to_local.py --dry-run

# Full migration (backs up existing local DB files first)
uv run python scripts/migrate_to_local.py
```

See `scripts/migrate_to_local.py --help` for all options.

## CrabHarness

[`crabharness/`](./crabharness/) is the mission-first control plane for
evidence collection. It plans what to crawl, delegates heavy work to plugin
workers, validates the collected bundle, and emits promotion packages.

Core responsibilities:

- Decide crawl target, scope, depth, volume, rate limits, and success criteria.
- Store every collected page, document, file, image, and log as evidence.
- Preserve hashes, source URLs or paths, crawl timestamps, parser status, and
  missing-context candidates.
- Promote only after completeness, semantic relevance, and autoresearch gates pass.

See the [CrabHarness README](./crabharness/README.md).

## MetaOntology OS

### 9 Spaces

| Space | Role |
| --- | --- |
| subject | Actors with identity, agency, roles, and permissions. |
| resource | Documents, datasets, tools, APIs, files, and projects. |
| evidence | Raw observations, logs, text units, parser/OCR outputs, and empirical records. |
| concept | Entities, concepts, topics, classes, and domain abstractions. |
| claim | Derived assertions grounded by evidence. |
| community | Clusters and summaries of related concepts or actors. |
| outcome | KPIs, risks, impacts, and measurable results. |
| lever | Tunable controls that affect outcomes or concepts. |
| policy | Access, sensitivity, approval, and governance rules. |

### Grammar Extensions

| from_space | to_space | relations added | purpose |
|---|---|---|---|
| `resource` | `concept` | `mentions`, `has_column` | Source documents reference or structurally define concepts. |
| `concept` | `outcome` | `can_derive_metric` | Concepts that can be computed into a measurable KPI or metric. |

### Core MCP Tools

- `ontology_manifest`: return the full grammar.
- `ontology_add_node`: add or update a grammar-validated node.
- `ontology_add_edge`: add a grammar-validated edge.
- `ontology_query`: hybrid vector + BM25 + graph query.
- `ontology_impact`: I1–I7 impact analysis.
- `ontology_rebac_check`: relationship-based access check.
- `ontology_ingest`: ingest text into the local ontology stores (vector + doc only).
- `ontology_extract`: LLM-extract nodes/edges from text and write to the full graph.
- `content_pack_list`: list all content packs by node count and title.
- `harness_promotion_apply`: apply a CrabHarness promotion package.

## OpenCrab Pack v1

LocalCrab exports ontology deliveries as OpenCrab Pack v1 ZIPs.

Required layout:

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

See [OpenCrab Pack v1 ZIP format](./docs/opencrab-pack-v1.md).

## Development

```bash
make dev-install
make seed
make test
make status
```

Run integration tests:

```bash
OPENCRAB_INTEGRATION=1 pytest tests/ -v
```

## Project Structure

```text
opencrab/
  grammar/        MetaOntology grammar, validator, glossary
  schemas/        YAML type schemas, schema packs, action schemas
  ontology/       builder, query, identity, canonicalization, promotion, ReBAC
  execution/      workflow and approval runtime
  stores/         Neo4j, Chroma, Mongo, SQL, LocalGraphStore, LocalSQLDocStore
  mcp/            MCP server and tool registry
crabharness/
  crabharness/    mission planner, runtime, validation, promotion package builder
  codex_workers/  plugin workers for crawlers and collectors
  missions/       example missions
docs/             integration and pack delivery contracts
```

## License

MIT.
