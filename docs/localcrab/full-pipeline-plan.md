# LocalCrab Full Pipeline Plan

This fork is the owner-operated LocalCrab distribution based on `AlexAI-MCP/OpenCrab`.

## Naming

- Product/runtime name: `localcrab`
- MCP server name: `localcrab`
- Harness CLI alias: `localcrab-harness`
- Local data root: `~/.openclaw/workspace/data/localcrab`
- Recommended venv: `~/.openclaw/workspace/.venvs/localcrab`

The upstream Python package may remain `opencrab` initially to reduce merge friction.
User-facing wrappers and deployment assets should use `localcrab`.

## Target full pipeline

1. Core ontology stores and MCP tools
   - local SQLite graph store
   - local Chroma vector store
   - local JSON document store
   - MCP stdio server exposed as `localcrab`

2. CrabHarness control plane
   - mission planning
   - worker registry
   - worker delegation
   - validation reports
   - promotion package generation and apply/dry-run

3. Crawling workers
   - HTTP/static-page workers
   - Playwright/browser workers
   - durable worker runtime layout
   - per-worker preflight checks

4. Evidence indexing
   - parser outputs
   - raw evidence files
   - source hashes and provenance
   - chunking and vector indexing

5. OCR
   - OCR adapter interface
   - default local OCR backend
   - OCR evidence schema with confidence and page/region metadata

6. CLIP/image context
   - image extraction adapter
   - CLIP/SigLIP-style embedding backend
   - image evidence records linked to source material

7. Neo4j validation/export
   - optional local or external Neo4j target
   - import/check workflow
   - `neo4j/opencrab_ingest.jsonl` export

8. Pack export
   - OpenCrab Pack v1 ZIP generation
   - quality report
   - sample queries and community report placeholders

9. Raspberry Pi 5 profile
   - Python 3.11 runtime profile
   - ARM64 dependency constraints
   - NVMe data-path assumptions
   - heavy OCR/CLIP/Neo4j jobs run on demand or externalized when needed

## Initial implementation order

1. Keep fork synchronized with upstream `AlexAI-MCP/OpenCrab`.
2. Add localcrab wrappers and fixed data-path configuration.
3. Make CrabHarness workers reproducible from a clean clone.
4. Add OCR and image-context adapter interfaces before choosing heavy defaults.
5. Wire Neo4j validation and pack export as explicit commands.
6. Add Pi 5 install notes and smoke tests.

## Non-goals for the first pass

- Renaming the upstream Python import package from `opencrab` to `localcrab`.
- Running all heavy services permanently on Raspberry Pi 5.
- Treating README-described OCR/CLIP/full factory behavior as complete until backed by tests.
