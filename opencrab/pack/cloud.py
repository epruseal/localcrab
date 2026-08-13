"""3-jsonl(nodes/edges/chunks) 디렉터리 → opencrab-cloud-pack-v1 ZIP.

**소비자: OpenCrab Cloud 업로드 파이프라인.** manifest 키는 `format`, 값은
`"opencrab-cloud-pack-v1"`. 입력은 nodes/edges/chunks.jsonl 3파일 평면 디렉터리
(예: 호출자의 `by-pack/{slug}/`). 산출물 레이어는 `graph/*.jsonl`
(정규화된 노드·엣지) + `cloud/*.jsonl`(documents·chunks).

**`opencrab.pack.assembler.assemble_pack_v1` 과는 별개 산출물이다 — 혼동 금지.**
그쪽 소비자는 로컬 그래프 스토어·neo4j 임포트 경로이고, manifest 키는
`format_version`, 값은 `"opencrab-pack-v1"`(`-cloud-` 없음), 입력은 neo4j
ingest jsonl·evidence 인덱스를 갖춘 스테이징 디렉터리, 산출물에는 이 모듈에 없는
neo4j 아티팩트·해시·품질 리포트가 있다. 교차 참조 0건 — 통합하지 마라.

호출자가 자신의 디렉터리 레이아웃(`by-pack/{slug}` 같은 저장 규칙)을 pack_dir
인자로 미리 해석해 넘긴다. 이 모듈은 그 규칙을 모른다(하드코딩된 base 경로 없음).
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from opencrab.pack.jsonl_io import iter_jsonl, jsonl_exists
from opencrab.pack.schema import absorb_legacy_top_level


def load_jsonl(path: Path) -> list[dict]:
    return list(iter_jsonl(path))  # shard-aware — ZIP 내부는 jsonl_bytes()로 단일 파일 재직렬화(포맷 무변경)


def jsonl_bytes(records: list[dict]) -> bytes:
    buf = io.StringIO()
    for r in records:
        buf.write(json.dumps(r, ensure_ascii=False) + "\n")
    return buf.getvalue().encode("utf-8")


def build_zip(pack_dir: str | Path, out_path: str | Path, pack_id: str = "", title: str = "") -> dict:
    """pack_dir(nodes/edges/chunks.jsonl 보유) → opencrab-cloud-pack-v1 ZIP.

    pack_dir 이름 자체가 팩 slug다 — 호출자가 `by-pack/{slug}` 같은 저장 규칙을
    미리 해석해 pack_dir로 넘긴다.
    """
    pack_dir = Path(pack_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)  # 호출자가 out_path 부모를 미리
    # 만들어 뒀다는 가정을 깬다 — 원래 CLI는 out_path 기본값이 늘 BASE_DIR(항상 존재)
    # 밑이라 이 문제가 드러나지 않았다. 같은 패키지의 assembler.assemble_pack_v1 도
    # 같은 방식으로 output.parent를 만든다(관례 일치).
    pack_slug = pack_dir.name

    # ── 필수 파일 존재 확인 ────────────────────────────────────────────
    nodes_path  = pack_dir / "nodes.jsonl"
    edges_path  = pack_dir / "edges.jsonl"
    chunks_path = pack_dir / "chunks.jsonl"

    if not jsonl_exists(nodes_path):
        sys.exit(f"ERROR: {nodes_path} 없음")

    nodes  = load_jsonl(nodes_path)
    edges  = load_jsonl(edges_path) if jsonl_exists(edges_path) else []
    chunks = load_jsonl(chunks_path) if jsonl_exists(chunks_path) else []

    # ── graph/nodes.jsonl — id 필드 있는 노드만 ────────────────────────
    graph_nodes = [n for n in nodes if n.get("id")]

    # ── graph/edges.jsonl — source/target 정규화, 끊긴 엣지 제외 ───────
    node_ids = {n["id"] for n in graph_nodes}
    graph_edges = []
    skipped_edges = 0
    for e in edges:
        src = e.get("source_id") or e.get("from_id") or e.get("from") or e.get("source")
        tgt = e.get("target_id") or e.get("to_id")   or e.get("to")   or e.get("target")
        if not src or not tgt:
            skipped_edges += 1
            continue
        if src not in node_ids or tgt not in node_ids:
            skipped_edges += 1
            continue
        graph_edges.append({
            "id":         e["id"],
            "source":     src,
            "target":     tgt,
            "label":      e.get("label") or e.get("relation") or "relates_to",
            "created_at": e.get("created_at"),
            "properties": e.get("properties", {}),
        })

    # ── cloud/documents.jsonl — resource + evidence/LogEntry 노드 → document
    # TextUnit은 vector chunks에 이미 있으므로 documents에서 제외
    cloud_documents = []
    for n in graph_nodes:
        if n.get("space") not in ("resource", "evidence"):
            continue
        if n.get("node_type") == "TextUnit":
            continue  # Q&A 청크는 cloud/chunks.jsonl로 충분
        # 레거시 호환: 2026-08-03 이전 생산자는 커스텀 필드(url·source_url 등)를
        # 노드 최상위에 펼쳤다. 로더(opencrab.pack.normalize.transform_node)와 같은
        # 정본 흡수 규칙(opencrab.pack.schema.absorb_legacy_top_level)을 써야
        # 두 소비자가 같은 노드에서 다른 필드를 보는 일이 없다 — 규칙을 여기서
        # 다시 선언하지 않는다. 중첩 properties 가 우선(정본 위치)한다.
        props = absorb_legacy_top_level(n)
        cloud_documents.append({
            "id":         n["id"],
            "title":      n.get("label", ""),
            "source":     props.get("source") or props.get("url") or n.get("label", ""),
            "source_url": props.get("source_url") or props.get("url") or "",
            "space":      "resource",
            "node_type":  n.get("node_type", "Document"),
            "pack_id":    pack_id or pack_slug,
            "created_at": n.get("created_at"),
            "properties": props,
        })

    # ── cloud/chunks.jsonl — 빈 텍스트 제외 ───────────────────────────
    cloud_chunks = []
    for c in chunks:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        cloud_chunks.append({
            "id":          c["id"],
            "document_id": c.get("document_id", ""),
            "text":        text,
            "source":      c.get("source", ""),
            "metadata":    c.get("metadata", {}),
        })

    # readable document 1개 이상 보장 (검증)
    if not cloud_chunks and not cloud_documents:
        sys.exit("ERROR: readable document/chunk 없음 — 빈 ZIP 생성 불가")

    # ── space 분포 ─────────────────────────────────────────────────────
    all_spaces = ["subject","resource","evidence","concept","claim",
                  "community","outcome","lever","policy"]
    space_dist = {s: sum(1 for n in graph_nodes if n.get("space") == s)
                  for s in all_spaces}

    # ── manifest.json ──────────────────────────────────────────────────
    manifest = {
        "format":      "opencrab-cloud-pack-v1",
        "pack_id":     pack_id or pack_slug,
        "title":       title or pack_slug,
        "created_at":  datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": {
            "nodes":          len(graph_nodes),
            "edges":          len(graph_edges),
            "edges_skipped":  skipped_edges,
            "documents":      len(cloud_documents),
            "chunks":         len(cloud_chunks),
        },
        "spaces": space_dist,
    }

    # ── ZIP 조립 ───────────────────────────────────────────────────────
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json",          json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        zf.writestr("graph/nodes.jsonl",      jsonl_bytes(graph_nodes))
        zf.writestr("graph/edges.jsonl",      jsonl_bytes(graph_edges))
        zf.writestr("cloud/documents.jsonl",  jsonl_bytes(cloud_documents))
        zf.writestr("cloud/chunks.jsonl",     jsonl_bytes(cloud_chunks))

    return manifest
