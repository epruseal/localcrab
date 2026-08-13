"""Assemble OpenCrab Pack v1 ZIP artifacts."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opencrab.common.hashing import file_sha256

_PACK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows), encoding="utf-8")




def _copy_if_exists(src: Path, dst: Path) -> bool:
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    return False


def _normalise_ingest_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for row in rows:
        kind = row.get("kind")
        payload = row.get("payload", row)
        if kind == "node":
            nodes.append(payload)
        elif kind == "edge":
            edges.append(payload)
        elif kind == "evidence":
            evidence.append(payload)
    return nodes, edges, evidence


def _evidence_ref_stats(rows: list[dict[str, Any]], evidence_ids: set[Any]) -> tuple[int, int, int]:
    """Return (total refs, unresolved refs, rows carrying at least one ref)."""
    total = 0
    missing = 0
    with_refs = 0
    for row in rows:
        refs = row.get("evidence_refs") or []
        if not refs:
            continue
        with_refs += 1
        total += len(refs)
        missing += sum(1 for ref in refs if ref not in evidence_ids)
    return total, missing, with_refs


def _ratio(total: int, missing: int) -> float | None:
    """Fraction resolved, or None when there's nothing to measure (never a
    fabricated 1.0 for an empty/uncomputable metric)."""
    if total == 0:
        return None
    return round((total - missing) / total, 4)


def _quality_report(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute pack quality metrics from the actual assembled data.

    Only metrics derivable from nodes/edges/evidence are reported. Metrics
    that would require pipeline-stage inputs this function doesn't receive
    (parsing/OCR/CLIP completeness, multi-hop path coverage) are dropped
    rather than emitted as fake constants -- see report to the caller for
    the audit trail of what changed.
    """
    evidence_ids = {row.get("evidence_id") or row.get("id") for row in evidence}
    node_ids = {row.get("id") for row in nodes}

    node_ref_total, node_ref_missing, _ = _evidence_ref_stats(nodes, evidence_ids)
    edge_ref_total, edge_ref_missing, edge_with_refs = _evidence_ref_stats(edges, evidence_ids)
    total_refs = node_ref_total + edge_ref_total
    total_missing = node_ref_missing + edge_ref_missing

    broken_edges = sum(1 for edge in edges if edge.get("from_id") not in node_ids or edge.get("to_id") not in node_ids)
    referenced_ids = {edge.get("from_id") for edge in edges} | {edge.get("to_id") for edge in edges}
    orphan_nodes = sum(1 for node in nodes if node.get("id") not in referenced_ids)

    status = "pass" if total_missing == 0 and broken_edges == 0 else "warn"
    return {
        "status": status,
        "summary": {
            "evidence_coverage": _ratio(total_refs, total_missing),
            "node_evidence_integrity": _ratio(node_ref_total, node_ref_missing),
            "edge_evidence_integrity": _ratio(edge_ref_total, edge_ref_missing),
            "relationship_evidence_coverage": round(edge_with_refs / len(edges), 4) if edges else None,
            "graph_reference_integrity": round((len(edges) - broken_edges) / len(edges), 4) if edges else None,
        },
        "checks": {
            "grammar": "not_run",
            "schema": "not_run",
            "evidence_refs": "pass" if total_missing == 0 else "warn",
            "orphan_nodes": "pass" if orphan_nodes == 0 else "warn",
            "broken_edges": "pass" if broken_edges == 0 else "fail",
            "neo4j_import": "not_run",
        },
        "counts": {
            "missing_evidence_refs": total_missing,
            "broken_edges": broken_edges,
            "orphan_nodes": orphan_nodes,
        },
        "issues": [],
    }


def _manifest(pack_id: str, title: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], evidence: list[dict[str, Any]], root: Path) -> dict[str, Any]:
    nodes_path = root / "graph/nodes.jsonl"
    edges_path = root / "graph/edges.jsonl"
    evidence_path = root / "evidence/index.jsonl"
    quality_path = root / "quality/report.json"
    neo4j_path = root / "neo4j/opencrab_ingest.jsonl"
    files = [path for path in root.rglob("*") if path.is_file()]
    return {
        "format_version": "opencrab-pack-v1",
        "pack_id": pack_id,
        "title": title,
        "version": "1.0.0",
        "grammar_version": "1.0.0",
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": "LocalCrab",
        "license": {"scope": "personal", "name": "unspecified"},
        "source": {"mode": "local", "label": title, "url": None, "description": "Assembled by LocalCrab."},
        "counts": {
            "documents": 0,
            "chunks": 0,
            "images": sum(1 for item in evidence if item.get("kind") == "image_context"),
            "evidence": len(evidence),
            "nodes": len(nodes),
            "edges": len(edges),
            "files": len(files),
            "bytes": sum(path.stat().st_size for path in files),
        },
        "limits": {"split_recommended": False, "staged_ingest_recommended": False, "reason": None},
        "quality": json.loads(quality_path.read_text(encoding="utf-8"))["summary"],
        "retrieval_hints": {"relation_cues": [], "benchmark_focus": ["relationship_questions", "multi_hop", "hallucination_guard"]},
        "hashes": {
            "nodes_sha256": file_sha256(nodes_path),
            "edges_sha256": file_sha256(edges_path),
            "evidence_sha256": file_sha256(evidence_path),
            "neo4j_opencrab_ingest_sha256": file_sha256(neo4j_path),
            "pack_sha256": None,
        },
        "artifacts": {
            "nodes": "graph/nodes.jsonl",
            "edges": "graph/edges.jsonl",
            "evidence_index": "evidence/index.jsonl",
            "quality_report": "quality/report.json",
            "neo4j_cypher": "neo4j/import.cypher",
            "opencrab_ingest": "neo4j/opencrab_ingest.jsonl",
            "neo4j_export_status": "neo4j/export_status.json",
        },
    }


def _stage_optional_dirs(source: Path, root: Path) -> None:
    """Copy the recommended optional staging directories when present."""
    for dirname in ["raw", "parsed", "ocr", "images", "clip", "scripts"]:
        src = source / dirname
        if src.exists():
            shutil.copytree(src, root / dirname)


def _load_graph_data(source: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], Path, list[dict[str, Any]]]:
    """Load nodes/edges/evidence, preferring dedicated graph/evidence files
    over the combined neo4j ingest JSONL when both are present."""
    ingest_src = source / "neo4j/opencrab_ingest.jsonl"
    ingest_rows = _read_jsonl(ingest_src)
    nodes, edges, evidence = _normalise_ingest_rows(ingest_rows)

    graph_nodes_src = source / "graph/nodes.jsonl"
    graph_edges_src = source / "graph/edges.jsonl"
    evidence_src = source / "evidence/index.jsonl"
    if graph_nodes_src.exists():
        nodes = _read_jsonl(graph_nodes_src)
    if graph_edges_src.exists():
        edges = _read_jsonl(graph_edges_src)
    if evidence_src.exists():
        evidence = _read_jsonl(evidence_src)
    return nodes, edges, evidence, ingest_src, ingest_rows


def _write_neo4j_artifacts(
    source: Path,
    root: Path,
    ingest_src: Path,
    ingest_rows: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    if not _copy_if_exists(ingest_src, root / "neo4j/opencrab_ingest.jsonl"):
        _write_jsonl(root / "neo4j/opencrab_ingest.jsonl", ingest_rows)
    _copy_if_exists(source / "neo4j/export_status.json", root / "neo4j/export_status.json")
    if not (root / "neo4j/export_status.json").exists():
        _write_json(root / "neo4j/export_status.json", {"status": "not_run", "nodes": len(nodes), "edges": len(edges)})
    if not _copy_if_exists(source / "neo4j/import.cypher", root / "neo4j/import.cypher"):
        (root / "neo4j/import.cypher").write_text("// Import graph/nodes.jsonl and graph/edges.jsonl into Neo4j before export.\n", encoding="utf-8")


def _write_static_artifacts(root: Path, title: str) -> None:
    _write_json(root / "sample_queries.json", {"queries": []})
    _write_json(root / "community_reports.json", {"reports": []})
    (root / "README.md").write_text(f"# {title}\n\nOpenCrab Pack v1 artifact assembled by LocalCrab.\n", encoding="utf-8")


def _zip_root(root: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())


def assemble_pack_v1(
    source_dir: str | Path,
    output_zip: str | Path,
    *,
    pack_id: str,
    title: str | None = None,
) -> dict[str, Any]:
    """Build an OpenCrab Pack v1 ZIP from a pack staging directory.

    **소비자: 로컬 그래프 스토어 재적재 · neo4j 임포트 경로.** manifest 키는
    `format_version`, 값은 `"opencrab-pack-v1"`(`-cloud-` 없음). 입력은
    `neo4j/opencrab_ingest.jsonl`(또는 `graph/`+`evidence/` 분리 파일)을 갖춘
    스테이징 디렉터리이고, 산출물에는 neo4j 아티팩트·해시·품질 리포트가 포함된다.

    **`opencrab.pack.cloud.build_zip` 과는 별개 산출물이다 — 혼동 금지.** 그쪽
    소비자는 OpenCrab Cloud 업로드 파이프라인이고, manifest 키는 `format`,
    값은 `"opencrab-cloud-pack-v1"`, 입력은 nodes/edges/chunks.jsonl 3파일 평면
    디렉터리다. 교차 참조 0건 — 통합하지 마라.
    """
    if not _PACK_ID_RE.fullmatch(pack_id) or ".." in pack_id:
        raise ValueError(f"invalid pack_id: {pack_id!r}")
    source = Path(source_dir).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    output = Path(output_zip).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    title = title or pack_id

    with tempfile.TemporaryDirectory(prefix="opencrab-pack-v1-") as tmp_name:
        root = Path(tmp_name) / pack_id
        root.mkdir(parents=True)

        _stage_optional_dirs(source, root)
        nodes, edges, evidence, ingest_src, ingest_rows = _load_graph_data(source)

        _write_jsonl(root / "graph/nodes.jsonl", nodes)
        _write_jsonl(root / "graph/edges.jsonl", edges)
        _write_jsonl(root / "evidence/index.jsonl", evidence)
        _write_json(root / "quality/report.json", _quality_report(nodes, edges, evidence))

        _write_neo4j_artifacts(source, root, ingest_src, ingest_rows, nodes, edges)
        _write_static_artifacts(root, title)

        manifest = _manifest(pack_id, title, nodes, edges, evidence, root)
        _write_json(root / "manifest.json", manifest)

        _zip_root(root, output)

    pack_sha = file_sha256(output)
    return {"status": "ok", "pack_id": pack_id, "output": str(output), "pack_sha256": pack_sha, "nodes": len(nodes), "edges": len(edges), "evidence": len(evidence)}
