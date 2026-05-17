# OpenCrab Pack v1 ZIP Format

OpenCrab Pack v1 is the delivery contract between LocalCrab and OpenCrab SaaS.

The ZIP must be useful in two places:

- Local reproducibility: a developer can inspect the evidence and replay the
  graph in Neo4j.
- Hosted ingestion: OpenCrab SaaS can read the normalized graph and evidence
  index without depending on private LocalCrab internals.

## Packaging Pipeline

```text
validate -> Neo4j import/check -> Neo4j graph export -> package
```

The ZIP is not just an archive. It is the promotion artifact.

## Required Layout

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

Optional but recommended:

```text
raw/
parsed/
ocr/
images/
clip/
scripts/import_to_neo4j.py
neo4j/import_status.json
neo4j/export_status.json
```

## `manifest.json`

The manifest identifies the pack and tells OpenCrab SaaS how to handle it.

Required fields:

```json
{
  "format_version": "opencrab-pack-v1",
  "pack_id": "example_pack",
  "title": "Example Ontology Pack",
  "version": "1.0.0",
  "grammar_version": "1.0.0",
  "created_at": "2026-05-17T00:00:00Z",
  "created_by": "LocalCrab",
  "license": {
    "scope": "personal",
    "name": "MIT"
  },
  "source": {
    "mode": "crawl",
    "label": "Example source",
    "url": "https://example.com",
    "description": "What was collected or provided."
  },
  "counts": {
    "documents": 0,
    "chunks": 0,
    "images": 0,
    "evidence": 0,
    "nodes": 0,
    "edges": 0,
    "files": 0,
    "bytes": 0
  },
  "limits": {
    "split_recommended": false,
    "staged_ingest_recommended": false,
    "reason": null
  },
  "quality": {
    "parsing_completeness": 1.0,
    "ocr_completeness": null,
    "clip_coverage": null,
    "evidence_coverage": 1.0,
    "chunk_coverage": 1.0,
    "node_evidence_integrity": 1.0,
    "edge_evidence_integrity": 1.0,
    "relationship_evidence_coverage": 1.0,
    "multihop_path_coverage": 1.0,
    "graph_reference_integrity": 1.0,
    "promotion_status": "validated"
  },
  "retrieval_hints": {
    "relation_cues": ["reason", "revision", "not_applicable", "risk", "law"],
    "benchmark_focus": ["relationship_questions", "multi_hop", "hallucination_guard"]
  },
  "hashes": {
    "nodes_sha256": "...",
    "edges_sha256": "...",
    "evidence_sha256": "...",
    "pack_sha256": "..."
  },
  "artifacts": {
    "nodes": "graph/nodes.jsonl",
    "edges": "graph/edges.jsonl",
    "evidence_index": "evidence/index.jsonl",
    "quality_report": "quality/report.json",
    "neo4j_cypher": "neo4j/import.cypher",
    "opencrab_ingest": "neo4j/opencrab_ingest.jsonl",
    "neo4j_export_status": "neo4j/export_status.json"
  }
}
```

## `graph/nodes.jsonl`

One JSON object per line.

Required fields:

```json
{
  "id": "node:example",
  "label": "Example",
  "space": "concept",
  "node_type": "Entity",
  "properties": {},
  "evidence_refs": ["evidence:example:chunk:001"],
  "quality": {
    "confidence": 0.95,
    "parser": "native",
    "promotion_status": "validated"
  }
}
```

Rules:

- `space` must exist in the MetaOntology grammar.
- `node_type` must be valid for the selected space or accepted by an installed
  schema pack.
- `id` must be stable inside the pack.
- `evidence_refs` should not be empty for promoted nodes.
- `properties` must not contain secrets or private SaaS-only fields.
- Relationship-heavy nodes should expose searchable fields such as `reason`,
  `rationale`, `revision_reason`, `not_applicable_reason`, `risk`, `law`,
  `standard`, or equivalent nested properties when those facts exist.

## `graph/edges.jsonl`

One JSON object per line.

Required fields:

```json
{
  "id": "edge:example:mentions:target",
  "from_id": "node:example",
  "to_id": "node:target",
  "from_space": "evidence",
  "to_space": "concept",
  "relation": "mentions",
  "confidence": 0.92,
  "evidence_refs": ["evidence:example:chunk:001"],
  "properties": {}
}
```

Rules:

- `from_id` and `to_id` must exist in `graph/nodes.jsonl`.
- `(from_space, to_space, relation)` must validate against the grammar.
- `evidence_refs` should not be empty for promoted edges.
- Broken edges must fail validation.
- Edges that explain changes, restrictions, risk, law, defect chains, or
  material compatibility should preserve relation-specific evidence. These
  edges are critical for relationship and multi-hop RAG benchmarks.

## `evidence/index.jsonl`

The evidence index is the traceability layer. Every source artifact, parser
output, OCR output, CLIP output, chunk, and graph reference should be reachable
from here.

Example:

```json
{
  "evidence_id": "evidence:example:chunk:001",
  "kind": "text_chunk",
  "source": {
    "url": "https://example.com/page",
    "path": null,
    "title": "Example page"
  },
  "hash": "sha256:...",
  "collected_at": "2026-05-17T00:00:00Z",
  "parser": {
    "status": "ok",
    "method": "native_html",
    "warnings": []
  },
  "ocr": null,
  "clip": null,
  "location": {
    "document_id": "doc:example",
    "page": null,
    "section": "Introduction",
    "chunk_index": 1
  },
  "links": {
    "document_id": "doc:example",
    "chunk_ids": ["chunk:example:001"],
    "node_ids": ["node:example"],
    "edge_ids": ["edge:example:mentions:target"]
  }
}
```

For OCR evidence, include engine, confidence, page or region id, low-confidence
spans, and pass number. For CLIP evidence, include image hash, embedding id,
caption or tags, and related chunk ids.

## `quality/report.json`

The quality report records why the pack is safe, unsafe, or not yet ready to
promote.

Required top-level fields:

```json
{
  "status": "pass",
  "summary": {
    "parsing_completeness": 1.0,
    "ocr_completeness": null,
    "clip_coverage": null,
    "evidence_coverage": 1.0,
    "chunk_coverage": 1.0,
    "node_evidence_integrity": 1.0,
    "edge_evidence_integrity": 1.0,
    "relationship_evidence_coverage": 1.0,
    "multihop_path_coverage": 1.0,
    "graph_reference_integrity": 1.0
  },
  "checks": {
    "grammar": "pass",
    "schema": "pass",
    "evidence_refs": "pass",
    "orphan_nodes": "pass",
    "broken_edges": "pass",
    "neo4j_import": "pass"
  },
  "counts": {
    "missing_evidence_refs": 0,
    "broken_edges": 0,
    "orphan_nodes": 0,
    "parser_failures": 0,
    "ocr_low_confidence_spans": 0
  },
  "issues": []
}
```

Recommended defaults:

| Metric | Minimum |
| --- | --- |
| parsing completeness | 0.95 |
| OCR completeness for scanned/image docs | 0.90 |
| evidence coverage | 0.98 |
| chunk coverage | 0.95 |
| node evidence integrity | 1.00 |
| edge evidence integrity | 1.00 |
| relationship evidence coverage | 0.95 |
| multi-hop path coverage | 0.90 |
| graph reference integrity | 1.00 |

## `neo4j/import.cypher`

This file is for local reproducibility. It should be possible to load the pack
into Neo4j and verify graph counts and relationship integrity.

The Cypher import does not replace `graph/nodes.jsonl` and `graph/edges.jsonl`.
Those JSONL files are the canonical SaaS-ingestible graph.

## `neo4j/opencrab_ingest.jsonl`

This file is the normalized graph export extracted from Neo4j after
`neo4j/import.cypher` has been applied and checked.

It is not merely a copy of `graph/nodes.jsonl` and `graph/edges.jsonl`.
It records the graph state that actually passed Neo4j import/check, including
relationship types and properties as Neo4j loaded them.

Each line should be one of:

```json
{ "kind": "node", "payload": { "...": "..." } }
{ "kind": "edge", "payload": { "...": "..." } }
{ "kind": "evidence", "payload": { "...": "..." } }
```

The node and edge counts in `opencrab_ingest.jsonl` must match the canonical
graph files unless the quality report explicitly explains a filtered item.

Generate it with:

```bash
opencrab export-neo4j-pack \
  --pack-id my_pack_id \
  --output build/my_pack/neo4j/opencrab_ingest.jsonl
```

The command also writes `neo4j/export_status.json` with exported node and edge
counts, timestamp, pack filter, and output path.

## SaaS Limits and Splitting

`manifest.json` must record pack size and counts so OpenCrab SaaS can choose
normal ingest, staged ingest, or split-pack ingest.

Recommended warning thresholds:

| Dimension | Warning threshold |
| --- | --- |
| ZIP size | 100 MB |
| Nodes | 100,000 |
| Edges | 300,000 |
| Evidence rows | 500,000 |
| Files | 20,000 |

If a pack exceeds a threshold, set:

```json
{
  "limits": {
    "split_recommended": true,
    "staged_ingest_recommended": true,
    "reason": "Pack exceeds recommended SaaS ingest threshold for evidence rows."
  }
}
```

## Validation Failures

A pack must fail validation when:

- `manifest.json` is missing or has an unsupported `format_version`.
- Required graph or evidence files are missing.
- A node uses an invalid grammar space.
- An edge uses an invalid grammar relation.
- An edge references a missing node.
- A promoted node or edge has no evidence refs.
- Relationship claims do not preserve reason/revision/applicability evidence
  that exists in the source material.
- Multi-hop risk questions cannot traverse material, method, defect, standard,
  and law nodes where those source entities exist.
- `evidence/index.jsonl` references missing artifacts without explanation.
- The ZIP exceeds declared limits and does not request staged or split ingest.
- Neo4j import/check fails without an explicit failure reason.

## Marketplace Metadata

The pack should include human-facing metadata for opencrab.sh:

- `README.md`: public explanation, source, scope, quality status, and usage.
- `sample_queries.json`: starter questions for users and agents.
- `community_reports.json`: GraphRAG/community summaries when available.

These files help OpenCrab SaaS present the pack in marketplace and community
surfaces without exposing private SaaS implementation details.
