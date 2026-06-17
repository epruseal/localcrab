# LocalCrab 로컬 적재·검색 워크플로

LocalCrab은 품질 우선의 로컬 온톨로지 지식 서비스입니다. 문서·데이터를 9-space
MetaOntology 그래프로 적재하고, 벡터·BM25·그래프를 결합한 하이브리드 검색을 MCP로
제공합니다. OpenCrab Pack v1 ZIP 내보내기는 선택 기능입니다.

기존 CrabHarness와 MetaOntology 문법은 그대로 유지됩니다. 이 워크플로는 증거 완전성과
최종 적재 품질에 관한 운영 정책을 정의합니다.

## 적재 파이프라인

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

## make_vector_store 분기 (EMBEDDING_BACKEND)

LocalCrab의 `make_vector_store` 함수는 환경변수 `EMBEDDING_BACKEND`로 분기한다.

### `local` (기본값)

기존 ChromaStore(`opencrab_vectors`, minilm 384d)를 반환한다. 코드 무변경.

### `openai`

ChromaStore(`opencrab_vectors_kure`, 1024d)와 ResilientEmbeddingFunction을 반환한다.
KURE-v1(한국어 특화, 1024d)을 기본 모델로 사용한다.

```
ResilientEF = OpenAIEF(primary) + LlamaCppEF(fallback, lazy load, 자동 다운로드)
```

- **OpenAIEF (primary)**: OpenAI 호환 임베딩 서버(LM Studio 등)에 요청.
- **LlamaCppEF (fallback)**: primary 실패 시 lazy load. 모델 파일이 없으면 자동 다운로드 후 llama-cpp-python으로 직접 임베딩.

### 워크플로 호환성

- `run_ingest_workflow.py`는 subprocess 오케스트레이터라 스토어를 직접 다루지 않는다.
  하위 `load_local_packs.py`가 `EMBEDDING_BACKEND` env를 따라가므로 자동 KURE 사용.
- **Chroma PersistentClient 단일 프로세스 락**: backfill 중 게이트웨이는 중단 필요.
  backfill 완료 후 게이트웨이를 재기동하면 `opencrab_vectors_kure` 컬렉션에 연결된다.

---

## Promotion Rule

LocalCrab should be conservative:

> If evidence traceability is incomplete, keep the ontology in candidate or
> validated state. Promote only when missing context is explained or repaired.

That is the key difference between a casual graph export and a complete
LocalCrab ontology ingestion.
