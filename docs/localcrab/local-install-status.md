# LocalCrab Local Install Status

Date: 2026-05-18
Branch: `localcrab/full-pipeline`

## Local paths

- Repo: `~/.openclaw/workspace/vendor/localcrab`
- Virtualenv: `~/.openclaw/workspace/.venvs/localcrab`
- Data root: `~/.openclaw/workspace/data/localcrab`
- OpenCrab wrapper: `scripts/localcrab`
- CrabHarness wrapper: `scripts/localcrab-harness`

## Naming

The upstream Python package/entry point is still `opencrab` to reduce merge friction.
The local runtime identity is set by wrapper environment variables:

- `MCP_SERVER_NAME=localcrab`
- `MCP_SERVER_VERSION=0.1.0-localcrab`

## Installed components

Installed into the `localcrab` virtualenv:

- `opencrab` editable install with dev extras
- `crabharness` editable install with dev extras

## Verified smoke tests

Core LocalCrab:

- `localcrab status`: OK for local SQLite graph, ChromaDB, JSON docs, SQLite SQL
- `localcrab manifest`: rendered MetaOntology grammar
- MCP stdio initialize/list tools: OK, 30 tools
- pytest: `128 passed, 3 skipped`

CrabHarness:

- `localcrab-harness catalog`: 3 workers found
  - `codex.landscape.scan`
  - `codex.github.trending`
  - `codex.soeak.detail`
- `localcrab-harness plan crabharness/missions/examples/landscape-construction-ai-usecases.json`: OK, 1 job

Neo4j validation/export:

- Existing Neo4j reachable at `bolt://localhost:7687`
- Auth verified with `neo4j/opencrab`
- Smoke pack created 2 nodes and 1 edge, then exported via `localcrab export-neo4j-pack`
- Export output contained 2 node records and 1 edge record
- Smoke nodes were removed from Neo4j after export validation

## 임베딩 백엔드 (2026-06-14 갱신)

- ✅ KURE-v1 임베딩 도입 완료 (feat/kure-embedding 브랜치)
- ✅ LM Studio text-embedding-kure-v1 (1024d, Q8_0) — 주력
- ✅ 로컬 GGUF /home/asdf/models/KURE-v1-Q8_0.gguf (605MB) — 폴백
- ✅ llama-cpp-python 0.3.29 설치
- ✅ backfill_kure.py — 기존 노드 KURE 컬렉션 적재
- ✅ systemd 유닛 3개(gateway/api/tunnel)에 localcrab-kure.env 적용
- ✅ 벡터 일치도: LM Studio↔로컬 cosine 0.999853 확인
- 기존 minilm 컬렉션(opencrab_vectors) 보존 — EMBEDDING_BACKEND=local 로 즉시 롤백

롤백 방법:
  /home/asdf/.openclaw/localcrab-kure.env 에서 EMBEDDING_BACKEND=local 후
  systemctl --user daemon-reload && systemctl --user restart localcrab-gateway

## Current limitations

This completes LocalCrab stage 1-2 only:

1. Core Python packages + CrabHarness installed and verified.
2. Neo4j-backed pack snapshot export verified.

The README-level full pipeline still needs additional work:

- reproducible `worker_runtime` for Playwright workers
- OCR adapter and default backend
- CLIP/image-context adapter and default backend
- complete OpenCrab Pack v1 ZIP assembly command
- Raspberry Pi 5 Python 3.11/ARM64 profile
