<p align="center">
  <img src="logo.png" alt="OpenCrab Logo" width="260"/>
</p>

# OpenCrab

**LocalCrab builds. OpenCrab SaaS distributes.**

OpenCrab is the public integration repository for the LocalCrab ontology
factory and the OpenCrab hosted ecosystem at [opencrab.sh](https://opencrab.sh).

This repository contains the local engine: MetaOntology OS grammar, MCP tools,
CrabHarness evidence collection, local stores, promotion lifecycle, and pack
export contracts. It does **not** contain the private implementation of the
hosted `opencrab.sh` SaaS product.

Any sample app or API code in this repository is local/demo infrastructure for
developer testing. It is not the production `opencrab.sh` SaaS code.

## What This Repo Is For

| Layer | Role | Lives here? |
| --- | --- | --- |
| LocalCrab | Local ontology factory for crawling, parsing, evidence indexing, Neo4j validation, and ZIP pack export. | Yes |
| CrabHarness | Mission-first control plane for crawler planning, worker delegation, evidence validation, and promotion packages. | Yes |
| MetaOntology OS | Canonical grammar, schemas, ReBAC, identity/canonicalization, promotion lifecycle, and MCP server tools. | Yes |
| OpenCrab SaaS | Hosted ingestion, marketplace, profiles, MCP access, community, and paid/free pack circulation. | No, linked via [opencrab.sh](https://opencrab.sh) |

The intended flow:

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
Neo4j/Cypher validation
        |
        v
OpenCrab Pack v1 ZIP
        |
        v
opencrab.sh ingest + marketplace + ecosystem distribution
```

## LocalCrab and OpenCrab SaaS

LocalCrab is quality-first. It exists to produce ontology packs with strong
evidence coverage, traceable parsing, OCR/CLIP context, graph validation, and
promotion receipts.

OpenCrab SaaS is ecosystem-first. It exists to ingest packs, make them useful
to users and agents, distribute them through marketplace/community surfaces,
and expose hosted MCP access.

Read the full relationship model:

- [LocalCrab and OpenCrab SaaS relationship](./docs/localcrab-opencrab-relationship.md)
- [LocalCrab factory workflow](./docs/localcrab-factory-workflow.md)
- [OpenCrab Pack v1 ZIP format](./docs/opencrab-pack-v1.md)

## Quick Start

### 1. Install LocalCrab

```bash
pip install -e ".[dev]"
```

### 2. Run LocalCrab

```bash
opencrab serve
```

LocalCrab runs locally by default. It uses SQLite and a local Chroma
persistent store under `./opencrab_data`.

**Local mode store backends:**

| Role | Backend | File |
| --- | --- | --- |
| Graph | `LocalGraphStore` (SQLite BFS) | `opencrab_data/graph.db` |
| Document | `LocalSQLDocStore` (SQLite) | `opencrab_data/doc_store.db` |
| Vector | ChromaStore (local PersistentClient) | `opencrab_data/chroma/` |
| SQL | SQLStore (SQLite) | `opencrab_data/opencrab.db` |

See [Architecture](./docs/ARCHITECTURE.md) for the design rationale and the
[Phase 2 roadmap](./docs/ARCHITECTURE.md#phase-2-ladybugdb-graph-store) for
the planned LadybugDB graph store replacement.

**Docker backend (recommended for production use):**

Set `STORAGE_MODE=docker` to connect to external Neo4j, Chroma, MongoDB, and
PostgreSQL instances instead of the local SQLite/file fallbacks.

```bash
STORAGE_MODE=docker opencrab serve
```

> Without `STORAGE_MODE=docker`, the graph store falls back to a SQLite-backed
> `LocalGraphStore`. All MCP tools — including `content_pack_list`,
> `ontology_query`, `ontology_lever_simulate`, `ontology_rebac_check`, and
> `export` — are fully supported in local mode via native SQLite queries.
>
> **SQLite version requirement:** Local mode uses `json_extract()` which
> requires **SQLite 3.9.0 or later** (released 2015-10-14). The system SQLite
> version must meet this minimum. Check with `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`.
>
> **Note on `ontology_rebac_check` in local mode:** Graph-based permission
> traversal uses Python BFS via `find_neighbors()` instead of Cypher. Direct
> and transitive (member_of/manages → permission relation) access paths are
> fully supported. Complex multi-hop patterns beyond depth 2 are not.

### 3. Verify the grammar and query path

```bash
opencrab status
opencrab manifest
opencrab query "system performance and error rates"
```

### 4. Add LocalCrab as an MCP server

```bash
claude mcp add opencrab -- opencrab serve
```

Or add it manually:

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

LocalCrab exposes a stdio MCP server. To access it from remote devices
(e.g. over Tailscale), bridge it to streamableHttp using
[supergateway](https://github.com/supermachineai/supergateway):

```bash
STORAGE_MODE=docker npx -y supergateway \
  --outputTransport streamableHttp \
  --port 8765 \
  --stdio "python -m opencrab.cli serve"
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

> Local mode (`STORAGE_MODE=local`) is suitable for single-machine use.
> All MCP tools including `ontology_rebac_check` and keyword search work
> in local mode via SQLite-native implementations. Set `STORAGE_MODE=docker`
> only when connecting to external Neo4j/MongoDB/PostgreSQL services.

## Migrating from Docker to Local Mode

If you have existing data in the docker backend (Neo4j + MongoDB + PostgreSQL
+ HTTP Chroma) and want to migrate to local mode:

```bash
# Dry-run: check connections and data counts, no writes
uv run python scripts/migrate_to_local.py --dry-run

# Full migration (backs up existing local DB files first)
uv run python scripts/migrate_to_local.py

# Switch to local mode
# Edit .env: STORAGE_MODE=local
# Then: opencrab serve
```

See `scripts/migrate_to_local.py --help` for all options.

## CrabHarness

[`crabharness/`](./crabharness/) is the mission-first control plane for
evidence collection. It plans what to crawl, delegates heavy work to plugin
workers, validates the collected bundle, and emits OpenCrab-ready promotion
packages.

Core responsibilities:

- Decide crawl target, scope, depth, volume, rate limits, and success criteria.
- Store every collected page, document, file, image, and log as evidence.
- Preserve hashes, source URLs or paths, crawl timestamps, parser status, and
  missing-context candidates.
- Promote only after completeness, semantic relevance, and autoresearch gates
  pass.

See the [CrabHarness README](./crabharness/README.md).

## MetaOntology OS

LocalCrab keeps the existing MetaOntology OS grammar and MCP surface as the
canonical ontology contract.

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

The following META_EDGES have been added to `opencrab/grammar/manifest.py`
beyond the original set:

| from_space | to_space | relations added | purpose |
|---|---|---|---|
| `resource` | `concept` | `mentions`, `has_column` | Source documents reference or structurally define concepts (keyword extraction, schema columns). |
| `concept` | `outcome` | `can_derive_metric` | Concepts that can be computed into a measurable KPI or metric. |

### Core MCP Tools

- `ontology_manifest`: return the full grammar.
- `ontology_add_node`: add or update a grammar-validated node.
- `ontology_add_edge`: add a grammar-validated edge.
- `ontology_query`: hybrid vector + BM25 + graph query.
- `ontology_impact`: I1-I7 impact analysis.
- `ontology_rebac_check`: relationship-based access check.
- `ontology_ingest`: ingest text into the local ontology stores (vector + doc only).
- `ontology_extract`: LLM-extract nodes/edges from text and write to the full graph. Supports `backend="cli"` to use the local `claude -p` CLI (subscription auth, no API key required) or `backend="api"` for direct Anthropic SDK calls.
- `content_pack_list`: list all content packs loaded in Neo4j (`pack_id`, node count, title). Unlike `schema_pack_list`, this reflects actual ingested content nodes.
- `harness_promotion_apply`: apply a CrabHarness promotion package.

## OpenCrab Pack v1

LocalCrab exports ontology deliveries as an OpenCrab Pack v1 ZIP. The pack is
designed to be recognized by OpenCrab SaaS while remaining reproducible in a
local Neo4j environment.

Required high-level layout:

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

The packaging pipeline is:

```text
validate -> Neo4j import/check -> Neo4j graph export -> normalized SaaS export -> ZIP package
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
  billing/        local usage hooks
  stores/         Neo4j, Chroma, Mongo, SQL, LocalGraphStore, LocalSQLDocStore
  mcp/            MCP server and tool registry
crabharness/
  crabharness/    mission planner, runtime, validation, promotion package builder
  codex_workers/  plugin workers for crawlers and collectors
  missions/       example missions
docs/             public integration and pack delivery contracts
```

## Korean Summary

이 리포지토리는 LocalCrab과 OpenCrab SaaS를 하나의 제품처럼 설명하는 공개 통합
리포지토리입니다. LocalCrab은 온톨로지 공장입니다. 크롤링, 파싱, OCR, CLIP
이미지 컨텍스트, evidence 풀 인덱싱, Neo4j 검증, ZIP 팩 생성을 담당합니다.

OpenCrab SaaS는 [opencrab.sh](https://opencrab.sh)의 생태계 허브입니다. 완성된
팩을 인제스트하고, 마켓플레이스와 커뮤니티에서 배포하며, hosted MCP 접근을
제공합니다. 단, `opencrab.sh`의 내부 SaaS 코드는 이 공개 리포지토리에 포함하지
않습니다.

## License

MIT.
