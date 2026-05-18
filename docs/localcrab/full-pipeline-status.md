# LocalCrab Full Pipeline Status

Date: 2026-05-18
Branch: `localcrab/full-pipeline`

Pi portability work is explicitly out of scope for this phase.

## Completed in this phase

### Worker runtime

- Added `crabharness/worker_runtime/` as the pinned Node worker runtime.
- Installed npm dependencies and generated `package-lock.json`.
- Runtime command for the SOEAK worker:
  - `npm --prefix ./worker_runtime run worker:soeak -- ...`
- The runtime uses `tsx` and `playwright`.
- Chromium was installed locally with Playwright under the user cache.
- Added SOEAK adapter `doctor()` so `localcrab-harness doctor soeak` checks:
  - node
  - npm
  - worker runtime directory
  - runtime package
  - runtime node_modules
  - worker script
  - Playwright module import

Verified:

```bash
scripts/localcrab-harness doctor soeak
cd crabharness && npm --prefix ./worker_runtime run worker:soeak -- --dry-run --limit 1
```

The dry run returns rc=0. Current local dataset has no matching rows, so the worker prints `No matching rows.`

### OCR adapter

Added `opencrab.media.ocr` and CLI command:

```bash
scripts/localcrab ocr PATH --backend auto --output evidence.json
```

Backends:

- `auto`: try EasyOCR first, then Tesseract, then fallback.
- `easyocr`: CPU-capable Korean/English OCR via `easyocr.Reader(..., gpu=False)`.
- `tesseract`: require Tesseract/Pillow path.
- `metadata`: deterministic metadata-only OCR evidence.

Verified local runtime:

- Reproducible requirements file: `requirements/localcrab-media.txt`.
- `torch==2.5.1+cpu` and `torchvision==0.20.1+cpu` from the PyTorch CPU index.
- `easyocr==1.7.2` with Korean/English model cache.
- Synthetic sample extracted `LOCALCRAB OCR TEST 123` at confidence `0.812` and `한글 테스트 456` at confidence `0.724`.

The fallback remains intentional. It keeps pack traceability working if a production OCR backend is unavailable.

### CLIP/image context adapter

Added `opencrab.media.image_context` and CLI command:

```bash
scripts/localcrab image-context PATH --backend auto --output evidence.json
```

Backends:

- `auto`: try `sentence-transformers`, then fallback.
- `sentence-transformers`: semantic image embedding with `clip-ViT-B-32`.
- `fingerprint`: deterministic local image fingerprint based on image/file features.

Verified local runtime:

- Reproducible requirements file: `requirements/localcrab-media.txt`.
- `sentence-transformers==5.5.0` with `clip-ViT-B-32`.
- Synthetic sample produced a 512-dimensional normalized image embedding.
- Text/image similarity scores were computed successfully.

The default fallback is not semantic CLIP. It is a stable local image-context placeholder that keeps the pack evidence format complete when the semantic model is unavailable.

### OpenCrab Pack v1 ZIP assembly

Added `opencrab.pack.assembler` and CLI command:

```bash
scripts/localcrab assemble-pack-v1 STAGING_DIR \
  --pack-id PACK_ID \
  --title "Pack Title" \
  --output build/PACK_ID.zip
```

The assembler creates the required Pack v1 layout:

- `manifest.json`
- `graph/nodes.jsonl`
- `graph/edges.jsonl`
- `evidence/index.jsonl`
- `quality/report.json`
- `neo4j/import.cypher`
- `neo4j/opencrab_ingest.jsonl`
- `neo4j/export_status.json`
- `README.md`
- `sample_queries.json`
- `community_reports.json`

It can build graph files from `neo4j/opencrab_ingest.jsonl` when canonical graph JSONL files are absent.

## Current status

LocalCrab now has the original non-Pi pipeline skeleton wired end to end:

```text
CrabHarness worker runtime
  -> worker artifacts / promotion package
  -> Neo4j export JSONL
  -> OCR/image-context evidence adapters
  -> OpenCrab Pack v1 ZIP assembly
```

## Remaining quality upgrades

These are implementation-quality upgrades, not missing command surfaces:

- Replace OCR fallback with a chosen production OCR backend and language pack policy.
- Replace image fingerprint fallback with a pinned semantic CLIP backend if semantic image retrieval is required locally.
- Add a first-class command that runs mission -> promotion apply -> Neo4j export -> pack ZIP in one invocation.
- Add richer graph/evidence validation against the full Pack v1 schema.


## Verified large dataset pack build

Built and ingested a full Pack v1 for `nvidia/Nemotron-Personas-Korea` at revision `d0a9272116a2ebf139b964ca72b8b8f604616689`.

Local paths:

- Dataset snapshot: `~/.openclaw/workspace/data/localcrab/datasets/nvidia-nemotron-personas-korea`
- Pack stage: `~/.openclaw/workspace/data/localcrab/packs/nvidia-nemotron-personas-korea/stage`
- Pack ZIP: `~/.openclaw/workspace/data/localcrab/packs/nvidia-nemotron-personas-korea/nvidia-nemotron-personas-korea.opencrab-pack-v1.zip`

Verified counts:

- Rows: `1,000,000`
- Source-file evidence: `22`
- Evidence records: `1,000,022`
- Graph nodes: `2,000,001`
- Graph edges: `2,000,000`
- `neo4j/opencrab_ingest.jsonl` lines: `5,000,023`
- Canonical `neo4j/opencrab_ingest.jsonl` lines: `4,000,001`
- ZIP entries: `23`
- ZIP SHA-256: `41d81a6c07563f44be588dd1cc3c06a40ec3b2081aa07c8d23aae7a8d4dd1ac4`
- Canonical Neo4j export SHA-256: `ea3314d54f1c5912e8bae09c84fe08b8e9a0ab531bf7de1dbf5e03afd5958735`

Neo4j ingest/export checks:

- Neo4j nodes: `Document=1`, `Persona=1,000,000`, `Evidence=1,000,000`
- Neo4j relationships: `CONTAINS=1,000,000`, `SUPPORTS=1,000,000`
- Evidence nodes hydrated with text/hash/source metadata: `1,000,000`
- Unhydrated Evidence nodes: `0`
- Missing node evidence refs: `0`
- Missing edge evidence refs: `0`
- ZIP `testzip`: pass

The pack builder is `scripts/build_nemotron_personas_korea_pack.py`; Neo4j import/export scripts are `scripts/import_pack_graph_to_neo4j.py` and `scripts/export_pack_graph_from_neo4j.py`; build dependencies are listed in `requirements/localcrab-pack-build.txt`.
