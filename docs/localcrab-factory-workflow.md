# LocalCrab Factory Workflow

LocalCrab is quality-first. Its job is to deliver ontology packs with complete
evidence coverage, traceable parsing, strong OCR/image handling, graph
validation, and promotion receipts.

The existing CrabHarness and MetaOntology grammar stay in place. This workflow
adds a stricter operating policy around evidence completeness and final
promotion quality.

## Factory Pipeline

```text
plan -> collect -> parse -> index evidence -> extract graph
     -> canonicalize -> validate quality -> verify in Neo4j
     -> export graph back from Neo4j -> export OpenCrab Pack v1 ZIP
```

## Path A: No Source Material Yet

Use this path when the user only knows the target domain or source area, but no
complete source corpus has been prepared.

### 1. Plan the Crawl

Create a CrabHarness mission before collecting anything.

The mission must freeze:

- Crawl target: domains, repositories, websites, datasets, APIs, or search
  spaces.
- Scope: allowed and excluded locations.
- Depth: link depth, folder depth, pagination depth, API expansion depth.
- Volume: max pages, files, bytes, images, records, and runtime.
- Rate limits: concurrency, delay, retry policy, robots/API constraints.
- Required evidence: pages, source files, documents, images, logs, metadata.
- Success criteria: minimum artifacts, required fields, semantic questions,
  minimum completeness score, and minimum semantic score.

### 2. Collect Evidence

Every crawler output is evidence, not just successful documents.

Store:

- Raw pages and files.
- Extracted text.
- Parser logs.
- Crawl logs.
- HTTP/API metadata.
- Images and media metadata.
- Failed or skipped URLs with reason codes.
- Missing-context candidates.

Each evidence artifact must include:

- Stable evidence id.
- Source URL or local path.
- Crawl timestamp.
- Content hash.
- Parser status.
- Media type.
- Byte size.
- Parent source or crawl run id.

### 3. Full Evidence Indexing

Before ontology extraction, build `evidence/index.jsonl`.

The index should connect:

- Source artifact -> parsed document.
- Parsed document -> chunks.
- Chunk -> extracted node ids.
- Chunk -> extracted edge ids.
- Image -> CLIP context ids.
- OCR page/region -> text chunks.
- Parser failure -> missing-context candidate.

No node or edge should be promoted without evidence refs.

### 4. Extract Under MetaOntology Grammar

Use the existing grammar path:

- `ontology_manifest` to inspect valid spaces and relations.
- `ontology_add_node` and `ontology_add_edge` semantics for graph shape.
- Schema registry for type-specific required fields.
- Identity and canonicalization tools for duplicate handling.
- Promotion lifecycle for candidate, validated, promoted, or rejected status.

### 5. Quality Gate

The final promotion gate must strongly weight:

- Parsing completeness.
- OCR completeness when applicable.
- Evidence coverage.
- Chunk coverage.
- Node evidence reference integrity.
- Edge evidence reference integrity.
- Orphan node count.
- Broken edge count.
- Missing source-map count.

Promotion should fail if graph entities cannot be traced back to evidence.

### 6. Neo4j Export Snapshot

After Cypher import/check succeeds, export the loaded graph back from Neo4j into
`neo4j/opencrab_ingest.jsonl`.

This gives the pack two graph views:

- `graph/nodes.jsonl` and `graph/edges.jsonl`: LocalCrab's canonical planned
  graph.
- `neo4j/opencrab_ingest.jsonl`: the actual graph snapshot after Neo4j accepted
  it.

If the counts diverge, `quality/report.json` must explain why.

## Path B: Source Material Already Exists

Use this path when the user provides files, folders, PDFs, images, exports, or
datasets.

### 1. Native Parsing First

Use native parsers when possible:

- Markdown, text, HTML, CSV, JSON, JSONL, XML, YAML.
- Office documents where text can be extracted.
- PDFs with embedded text.
- Structured datasets and metadata files.

Store parser output and parser status as evidence.

### 2. OCR Fallback and Double OCR

If a document cannot be read directly, or if it is scanned/image-heavy, run OCR
with at least two passes or engines.

The evidence index should preserve:

- OCR engine name.
- OCR version/config when available.
- Page or region id.
- Confidence score.
- Text output.
- Differences between OCR passes.
- Chosen merged text.
- Low-confidence spans.

Do not silently discard OCR disagreements. Treat them as reviewable evidence.

### 3. Image Context With CLIP

Image-heavy sources need visual context, not just OCR text.

For each image, record:

- Image hash and source path.
- CLIP embedding id.
- Captions or generated descriptions.
- Tags/classes.
- Nearby document context.
- Related page/section/chunk ids.

Image context can support concepts, claims, or evidence nodes, but it should
remain traceable to the original image.

### 4. Full Evidence Indexing

Every source item must be represented in `evidence/index.jsonl`, including:

- Native parser output.
- OCR output.
- CLIP image context.
- Chunk records.
- Node and edge references.
- Parser and OCR failures.
- Missing or skipped resources.

The central question for the final gate is:

> The material exists. Did any context disappear before graph promotion?

### 5. Quality Gate

Promotion requires:

- No unexplained missing source files.
- No promoted node without evidence refs.
- No promoted edge without evidence refs.
- No broken evidence refs.
- No broken node references in edges.
- OCR coverage for unreadable pages.
- CLIP context for meaningful image assets.
- Neo4j import/check success or an explicit failure reason.

## Recommended Quality Scores

Use these as defaults unless a mission explicitly sets stricter thresholds.

| Metric | Recommended minimum |
| --- | --- |
| Parsing completeness | 0.95 |
| OCR completeness for scanned/image docs | 0.90 |
| Evidence coverage | 0.98 |
| Chunk coverage | 0.95 |
| Node evidence ref integrity | 1.00 |
| Edge evidence ref integrity | 1.00 |
| Graph reference integrity | 1.00 |
| Broken evidence refs | 0 |
| Broken edges | 0 |

## Benchmark-Steered Quality Gates

LocalCrab packs should be evaluated against the failure modes that appear in
real RAG benchmarks, not only against schema validity.

The current priority benchmark weaknesses are:

- Relationship questions: "changed reason", "revision background",
  "why not applicable", and "company-specific restriction".
- Multi-hop questions: material + method + defect + law/regulation + risk.
- Fire/material questions: combinations that may fail fire performance and the
  evidence or regulation behind the risk.
- Hallucination traps: nonexistent methods, nonexistent standards, or
  unsupported installation rules.

For every pack, add benchmark steering checks before promotion:

- Important standards and construction methods must have explicit `reason`,
  `rationale`, `revision_reason`, or `not_applicable_reason` evidence when the
  source material contains that context.
- Nodes that represent materials, methods, defects, risks, or laws should be
  connected through evidence-backed edges, not left as isolated keyword hits.
- If a source says a method is unavailable, prohibited, deleted, restricted, or
  company-specific, that negative applicability must become a claim.
- Fire-performance and legal-risk claims must include evidence refs and should
  connect material nodes to law/standard nodes where source material permits.
- Missing information must be represented as absence with evidence, not filled
  by model inference.

These gates are designed so OpenCrab does not merely retrieve documents. It
retrieves the relationship structure that explains why the answer is true.

## Promotion Rule

LocalCrab should be conservative:

> If evidence traceability is incomplete, keep the ontology in candidate or
> validated state. Promote only when missing context is explained or repaired.

That is the key difference between a casual graph export and a LocalCrab
factory delivery.
