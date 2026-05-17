# LocalCrab and OpenCrab SaaS Relationship

OpenCrab is designed as one product with two deployment surfaces.

LocalCrab is the local ontology factory. OpenCrab SaaS, available at
[opencrab.sh](https://opencrab.sh), is the hosted ecosystem where finished
packs are ingested, distributed, installed, queried, and sold or shared.

The hosted SaaS implementation is private and is intentionally not included in
this public repository.

## Responsibilities

| System | Primary responsibility | Optimized for |
| --- | --- | --- |
| LocalCrab | Build high-quality ontology packs from local files, crawled sources, OCR/CLIP outputs, and Neo4j validation. | Quality, evidence coverage, reproducibility. |
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

> LocalCrab is the open ontology factory; OpenCrab SaaS is the hosted ecosystem.

For developers:

> Build and validate ontology packs locally, then bring them to opencrab.sh for
> hosted ingestion, distribution, and agent access.

For pack creators:

> LocalCrab gives you quality control before your ontology reaches users.

For SaaS users:

> OpenCrab SaaS lets you install, query, share, and monetize ontology packs
> without running the local factory.

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
