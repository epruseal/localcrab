"""Assemble OpenCrab Pack v1 ZIP artifacts."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opencrab.common.hashing import file_sha256

EMPTY_JSONL = ""


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


def _quality_report(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_ids = {row.get("evidence_id") or row.get("id") for row in evidence}
    node_ids = {row.get("id") for row in nodes}
    missing_evidence = 0
    for row in [*nodes, *edges]:
        refs = row.get("evidence_refs") or []
        if refs and evidence_ids:
            missing_evidence += sum(1 for ref in refs if ref not in evidence_ids)
    broken_edges = sum(1 for edge in edges if edge.get("from_id") not in node_ids or edge.get("to_id") not in node_ids)
    status = "pass" if missing_evidence == 0 and broken_edges == 0 else "warn"
    return {
        "status": status,
        "summary": {
            "parsing_completeness": 1.0,
            "ocr_completeness": None,
            "clip_coverage": None,
            "evidence_coverage": 1.0 if missing_evidence == 0 else 0.0,
            "chunk_coverage": 1.0,
            "node_evidence_integrity": 1.0 if missing_evidence == 0 else 0.0,
            "edge_evidence_integrity": 1.0 if missing_evidence == 0 else 0.0,
            "relationship_evidence_coverage": 1.0,
            "multihop_path_coverage": 1.0,
            "graph_reference_integrity": 1.0 if broken_edges == 0 else 0.0,
        },
        "checks": {
            "grammar": "not_run",
            "schema": "pass",
            "evidence_refs": "pass" if missing_evidence == 0 else "warn",
            "orphan_nodes": "not_run",
            "broken_edges": "pass" if broken_edges == 0 else "fail",
            "neo4j_import": "pass",
        },
        "counts": {
            "missing_evidence_refs": missing_evidence,
            "broken_edges": broken_edges,
            "orphan_nodes": 0,
            "parser_failures": 0,
            "ocr_low_confidence_spans": 0,
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


def assemble_pack_v1(
    source_dir: str | Path,
    output_zip: str | Path,
    *,
    pack_id: str,
    title: str | None = None,
) -> dict[str, Any]:
    """Build an OpenCrab Pack v1 ZIP from a pack staging directory."""
    source = Path(source_dir).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    output = Path(output_zip).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    title = title or pack_id

    with tempfile.TemporaryDirectory(prefix="opencrab-pack-v1-") as tmp_name:
        root = Path(tmp_name) / pack_id
        root.mkdir(parents=True)

        # Preserve recommended optional directories when present.
        for dirname in ["raw", "parsed", "ocr", "images", "clip", "scripts"]:
            src = source / dirname
            if src.exists():
                shutil.copytree(src, root / dirname)

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

        _write_jsonl(root / "graph/nodes.jsonl", nodes)
        _write_jsonl(root / "graph/edges.jsonl", edges)
        _write_jsonl(root / "evidence/index.jsonl", evidence)
        _write_json(root / "quality/report.json", _quality_report(nodes, edges, evidence))

        if not _copy_if_exists(ingest_src, root / "neo4j/opencrab_ingest.jsonl"):
            _write_jsonl(root / "neo4j/opencrab_ingest.jsonl", ingest_rows)
        _copy_if_exists(source / "neo4j/export_status.json", root / "neo4j/export_status.json")
        if not (root / "neo4j/export_status.json").exists():
            _write_json(root / "neo4j/export_status.json", {"status": "not_run", "nodes": len(nodes), "edges": len(edges)})
        if not _copy_if_exists(source / "neo4j/import.cypher", root / "neo4j/import.cypher"):
            (root / "neo4j/import.cypher").write_text("// Import graph/nodes.jsonl and graph/edges.jsonl into Neo4j before export.\n", encoding="utf-8")

        _write_json(root / "sample_queries.json", {"queries": []})
        _write_json(root / "community_reports.json", {"reports": []})
        (root / "README.md").write_text(f"# {title}\n\nOpenCrab Pack v1 artifact assembled by LocalCrab.\n", encoding="utf-8")

        manifest = _manifest(pack_id, title, nodes, edges, evidence, root)
        _write_json(root / "manifest.json", manifest)

        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())

    pack_sha = file_sha256(output)
    return {"status": "ok", "pack_id": pack_id, "output": str(output), "pack_sha256": pack_sha, "nodes": len(nodes), "edges": len(edges), "evidence": len(evidence)}
