# LocalCrab and OpenCrab SaaS Relationship

LocalCrab은 **[AlexAI-MCP/OpenCrab](https://github.com/AlexAI-MCP/OpenCrab)** 기반
로컬 배포판 fork입니다. 패키지명·엔트리포인트는 upstream 머지 충돌을 줄이려 `opencrab`을
유지합니다.

LocalCrab is a standalone local ontology service — it runs on a single machine
without Docker using SQLite and a local Chroma client. OpenCrab SaaS, available at
[opencrab.sh](https://opencrab.sh), is a separate hosted ecosystem where packs are
ingested, distributed, installed, queried, and shared.

The hosted SaaS implementation is private and is intentionally not included in
this public repository.

## Responsibilities

| System | Primary responsibility | Optimized for |
| --- | --- | --- |
| LocalCrab | Run a local ontology knowledge service — load docs/data into a 9-space MetaOntology graph and serve hybrid search (vector + BM25 + graph) via MCP. Optionally export OpenCrab Pack v1 ZIPs. | Local-first, no Docker, quality, reproducibility. |
| OpenCrab SaaS | Ingest OpenCrab packs, manage users and profiles, expose hosted MCP access, and distribute packs through marketplace/community surfaces. | Ecosystem growth, distribution, hosted usability. |
| GitHub repository | Provide public grammar, LocalCrab runtime, CrabHarness, pack format, examples, and developer onboarding. | International developer access and trust. |

## Boundary

This repository may contain:

- LocalCrab MCP server code.
- MetaOntology OS grammar, schemas, validators, stores, and local runtime.
- CrabHarness crawler planning and evidence validation workflow.
- OpenCrab Pack v1 delivery contract.
- Example missions, example packs, and public documentation.
- Links and calls to action for [opencrab.sh](https://opencrab.sh).

This repository must not contain:

- Private hosted SaaS business logic.
- Production `opencrab.sh` deployment secrets.
- SaaS billing, profile, marketplace, or community implementation details that
  are not intended to be public.
- Private customer packs or source corpora.

Any local app or API surface in this repository should be treated as demo or
developer infrastructure. It must not be described as the production
`opencrab.sh` implementation.

## Data Flow

```text
1. Source selection
   - Existing local files, PDFs, images, datasets, or a crawl target.

2. CrabHarness mission planning
   - Target, scope, depth, volume, rate limit, and success criteria are frozen.

3. Evidence collection
   - Pages, documents, files, images, logs, parser outputs, OCR outputs, and
     CLIP image context are stored as evidence artifacts.

4. Full evidence indexing
   - Every evidence item receives a stable id, source reference, timestamp,
     hash, parser status, and links to chunks, nodes, and edges.

5. MetaOntology extraction
   - Nodes and edges are extracted under the existing grammar.

6. Canonicalization and promotion
   - Duplicate candidates, aliases, evidence-backed claims, and promotion
     status are handled before final delivery.

7. Neo4j validation
   - Cypher import/check confirms graph integrity and reproducibility.

8. OpenCrab Pack v1 ZIP
   - A SaaS-ingestible ZIP is produced.

9. OpenCrab SaaS ingestion
   - The pack is uploaded or imported into opencrab.sh for hosted use,
     marketplace distribution, community discovery, and MCP access.
```

## Public Positioning

Use this sentence when explaining the system publicly:

> LocalCrab is an open-source local ontology knowledge service; OpenCrab SaaS is the hosted ecosystem.

For developers:

> Run LocalCrab locally to build a knowledge graph and serve hybrid search via MCP.
> Optionally export packs to opencrab.sh for hosted distribution and agent access.

For pack creators:

> LocalCrab gives you local quality control and evidence traceability before
> your ontology reaches users.

For SaaS users:

> OpenCrab SaaS lets you install, query, share, and monetize ontology packs
> without running the local service.

## Compatibility Rule

LocalCrab and OpenCrab SaaS should communicate through stable artifacts, not
private implementation coupling. The canonical bridge is OpenCrab Pack v1:

- `manifest.json` declares the pack identity, counts, limits, grammar version,
  hashes, and quality summary.
- `graph/nodes.jsonl` and `graph/edges.jsonl` are the SaaS-ingestible graph.
- `evidence/index.jsonl` preserves traceability from source artifacts to graph
  entities.
- `quality/report.json` records whether the pack is safe to promote.
- `neo4j/import.cypher` preserves local reproducibility.
- `neo4j/opencrab_ingest.jsonl` is extracted back out of Neo4j after
  import/check, so the pack includes the actual graph snapshot that passed
  verification.

This keeps LocalCrab useful as open source while letting OpenCrab SaaS evolve
as the hosted ecosystem.
