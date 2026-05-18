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
