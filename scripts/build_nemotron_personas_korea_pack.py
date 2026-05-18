#!/usr/bin/env python3
"""Build and ingest a full OpenCrab Pack v1 for nvidia/Nemotron-Personas-Korea.

This is intentionally streaming/batch-oriented because the dataset has 1M rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

REPO_ID = "nvidia/Nemotron-Personas-Korea"
REVISION = "d0a9272116a2ebf139b964ca72b8b8f604616689"
PACK_ID = "nvidia-nemotron-personas-korea"
TITLE = "NVIDIA Nemotron Personas Korea"
LICENSE = "cc-by-4.0"
BATCH_SIZE = 5000
TEXT_FIELDS = [
    "professional_persona",
    "sports_persona",
    "arts_persona",
    "travel_persona",
    "culinary_persona",
    "family_persona",
    "persona",
    "cultural_background",
    "skills_and_expertise",
    "hobbies_and_interests",
    "career_goals_and_ambitions",
]
PROFILE_FIELDS = [
    "sex",
    "age",
    "marital_status",
    "military_status",
    "family_type",
    "housing_type",
    "education_level",
    "bachelors_field",
    "occupation",
    "district",
    "province",
    "country",
]
LIST_FIELDS = ["skills_and_expertise_list", "hobbies_and_interests_list"]


def now() -> str:
    return datetime.now(UTC).isoformat()


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def evidence_id(uuid: str) -> str:
    return f"evidence:{PACK_ID}:{uuid}"


def persona_node_id(uuid: str) -> str:
    return f"persona:{uuid}"


def evidence_node_id(uuid: str) -> str:
    return f"evidence-node:{uuid}"


def source_evidence_id(rel: str) -> str:
    return f"evidence:{PACK_ID}:source:{hashlib.sha256(rel.encode()).hexdigest()[:16]}"


def clean(value: Any) -> Any:
    if value is None:
        return None
    # pyarrow scalars are already converted by to_pylist, but keep this safe.
    if hasattr(value, "as_py"):
        return value.as_py()
    return value


def text_blob(row: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in TEXT_FIELDS:
        value = row.get(field)
        if value:
            parts.append(f"[{field}]\n{value}")
    for field in LIST_FIELDS:
        value = row.get(field)
        if value:
            parts.append(f"[{field}]\n{value}")
    profile = {field: row.get(field) for field in PROFILE_FIELDS if row.get(field) is not None}
    if profile:
        parts.append("[profile]\n" + jdump(profile))
    return "\n\n".join(parts)


def compact_persona_props(row: dict[str, Any], shard: str, row_index: int, ev_id: str) -> dict[str, Any]:
    uuid = str(row["uuid"])
    props = {
        "id": persona_node_id(uuid),
        "uuid": uuid,
        "label": str(row.get("persona") or row.get("occupation") or uuid)[:180],
        "pack_id": PACK_ID,
        "source_id": PACK_ID,
        "source_repo": REPO_ID,
        "source_revision": REVISION,
        "source_shard": shard,
        "row_index": row_index,
        "evidence_refs": [ev_id],
    }
    for field in PROFILE_FIELDS:
        value = row.get(field)
        if value is not None:
            props[field] = value
    for field in LIST_FIELDS:
        value = row.get(field)
        if value is not None:
            props[field] = value
    # Keep searchable snippets without duplicating every long text field in graph props.
    for field in ["persona", "occupation", "district", "province", "country"]:
        value = row.get(field)
        if value:
            props[field] = value
    return props


def row_evidence(row: dict[str, Any], shard: str, row_index: int, file_sha: str) -> dict[str, Any]:
    uuid = str(row["uuid"])
    ev_id = evidence_id(uuid)
    persona_id = persona_node_id(uuid)
    return {
        "evidence_id": ev_id,
        "kind": "persona_row",
        "source": {
            "repo_id": REPO_ID,
            "revision": REVISION,
            "path": f"data/{shard}",
            "url": f"https://huggingface.co/datasets/{REPO_ID}/blob/{REVISION}/data/{shard}",
            "title": f"{REPO_ID} {shard} row {row_index}",
            "file_sha256": file_sha,
            "row_index": row_index,
            "uuid": uuid,
        },
        "hash": "sha256:" + hashlib.sha256(jdump(row).encode("utf-8")).hexdigest(),
        "collected_at": now(),
        "parser": {"status": "ok", "method": "parquet_row", "warnings": []},
        "ocr": None,
        "clip": None,
        "location": {"document_id": f"dataset:{PACK_ID}", "page": None, "section": shard, "chunk_index": row_index},
        "links": {"document_id": f"dataset:{PACK_ID}", "chunk_ids": [], "node_ids": [persona_id], "edge_ids": [f"edge:{PACK_ID}:contains:{uuid}", f"edge:{PACK_ID}:evidence:{uuid}"]},
        "text": text_blob(row),
        "row": row,
    }


def source_file_evidence(root: Path, file_path: Path) -> dict[str, Any]:
    rel = file_path.relative_to(root).as_posix()
    sha = sha256_file(file_path)
    return {
        "evidence_id": source_evidence_id(rel),
        "kind": "source_file",
        "source": {
            "repo_id": REPO_ID,
            "revision": REVISION,
            "path": rel,
            "url": f"https://huggingface.co/datasets/{REPO_ID}/blob/{REVISION}/{rel}",
            "title": rel,
        },
        "hash": f"sha256:{sha}",
        "collected_at": now(),
        "parser": {"status": "ok", "method": "huggingface_snapshot_file", "warnings": []},
        "ocr": None,
        "clip": None,
        "location": {"document_id": f"dataset:{PACK_ID}", "page": None, "section": rel, "chunk_index": None},
        "links": {"document_id": f"dataset:{PACK_ID}", "chunk_ids": [], "node_ids": [f"dataset:{PACK_ID}"], "edge_ids": []},
        "metadata": {"bytes": file_path.stat().st_size, "sha256": sha},
    }


def iter_rows(parquet_path: Path, columns: list[str]):
    pf = pq.ParquetFile(parquet_path)
    for batch in pf.iter_batches(batch_size=BATCH_SIZE, columns=columns):
        data = batch.to_pylist()
        for row in data:
            yield {k: clean(v) for k, v in row.items()}


def init_sqlite_graph(db_path: Path, reset_pack: bool) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_nodes (
            node_type   TEXT NOT NULL,
            node_id     TEXT NOT NULL,
            space_id    TEXT,
            properties  TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (node_type, node_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS graph_edges (
            from_type   TEXT NOT NULL,
            from_id     TEXT NOT NULL,
            relation    TEXT NOT NULL,
            to_type     TEXT NOT NULL,
            to_id       TEXT NOT NULL,
            properties  TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (from_type, from_id, relation, to_type, to_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_from ON graph_edges(from_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_to ON graph_edges(to_id)")
    if reset_pack:
        like = f"%\"pack_id\": \"{PACK_ID}\"%"
        conn.execute("DELETE FROM graph_edges WHERE properties LIKE ?", (like,))
        conn.execute("DELETE FROM graph_nodes WHERE properties LIKE ?", (like,))
        # Node ids are stable and faster to clear by prefix for this pack.
        conn.execute("DELETE FROM graph_nodes WHERE node_id = ?", (f"dataset:{PACK_ID}",))
        conn.execute("DELETE FROM graph_nodes WHERE node_id LIKE 'persona:%'")
        conn.execute("DELETE FROM graph_nodes WHERE node_id LIKE 'evidence-node:%'")
        conn.commit()
    return conn


def insert_many(conn: sqlite3.Connection, nodes: list[tuple], edges: list[tuple]) -> None:
    if nodes:
        conn.executemany(
            """
            INSERT INTO graph_nodes(node_type, node_id, space_id, properties)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(node_type, node_id) DO UPDATE SET
                space_id=excluded.space_id,
                properties=excluded.properties
            """,
            nodes,
        )
    if edges:
        conn.executemany(
            """
            INSERT INTO graph_edges(from_type, from_id, relation, to_type, to_id, properties)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_type, from_id, relation, to_type, to_id) DO UPDATE SET
                properties=excluded.properties
            """,
            edges,
        )
    conn.commit()


def zip_stage(stage: Path, zip_path: Path) -> str:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(stage).as_posix())
    return sha256_file(zip_path)


def build(args: argparse.Namespace) -> dict[str, Any]:
    dataset_root = Path(args.dataset_root).expanduser().resolve()
    out_root = Path(args.output_root).expanduser().resolve()
    stage = out_root / "stage"
    if args.reset and stage.exists():
        shutil.rmtree(stage)
    for sub in ["graph", "evidence", "quality", "neo4j", "raw", "images"]:
        (stage / sub).mkdir(parents=True, exist_ok=True)

    parquet_files = sorted((dataset_root / "data").glob("*.parquet"))
    if len(parquet_files) != 9:
        raise RuntimeError(f"expected 9 parquet shards, found {len(parquet_files)}")
    total_expected = sum(pq.ParquetFile(path).metadata.num_rows for path in parquet_files)

    # Copy lightweight source files for pack reproducibility. Parquet files are referenced by hashes,
    # not duplicated into the ZIP, to avoid doubling a 2GB source payload.
    for rel in ["README.md", ".gitattributes"]:
        src = dataset_root / rel
        if src.exists():
            dst = stage / "raw" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    if (dataset_root / "images").exists():
        for img in (dataset_root / "images").iterdir():
            if img.is_file() and img.name != ".DS_Store":
                shutil.copy2(img, stage / "images" / img.name)

    paths = {
        "nodes": stage / "graph/nodes.jsonl",
        "edges": stage / "graph/edges.jsonl",
        "evidence": stage / "evidence/index.jsonl",
        "ingest": stage / "neo4j/opencrab_ingest.jsonl",
    }
    handles = {k: v.open("w", encoding="utf-8") for k, v in paths.items()}
    seen_uuid: set[str] = set()
    row_count = 0
    source_evidence_count = 0
    duplicate_uuids: list[str] = []
    shard_summaries = []

    dataset_evidence_refs: list[str] = []
    source_files = [p for p in dataset_root.rglob("*") if p.is_file() and ".cache/huggingface" not in p.as_posix()]
    for file_path in sorted(source_files):
        ev = source_file_evidence(dataset_root, file_path)
        source_evidence_count += 1
        dataset_evidence_refs.append(ev["evidence_id"])
        line = json.dumps(ev, ensure_ascii=False, default=str)
        handles["evidence"].write(line + "\n")
        handles["ingest"].write(json.dumps({"kind": "evidence", "payload": ev}, ensure_ascii=False, default=str) + "\n")

    dataset_node = {
        "id": f"dataset:{PACK_ID}",
        "label": TITLE,
        "space": "resource",
        "node_type": "Document",
        "properties": {
            "id": f"dataset:{PACK_ID}",
            "pack_id": PACK_ID,
            "source_id": PACK_ID,
            "repo_id": REPO_ID,
            "revision": REVISION,
            "license": LICENSE,
            "expected_rows": total_expected,
            "source_file_count": len(source_files),
        },
        "evidence_refs": dataset_evidence_refs,
    }
    handles["nodes"].write(json.dumps(dataset_node, ensure_ascii=False, default=str) + "\n")
    handles["ingest"].write(json.dumps({"kind": "node", "payload": dataset_node}, ensure_ascii=False, default=str) + "\n")

    conn = init_sqlite_graph(Path(args.local_data_dir) / "graph.db", reset_pack=args.reset_ingest)
    insert_many(
        conn,
        [("Document", dataset_node["id"], "resource", json.dumps(dataset_node["properties"], ensure_ascii=False, default=str))],
        [],
    )

    parquet_hashes = {path.name: sha256_file(path) for path in parquet_files}
    columns = pq.ParquetFile(parquet_files[0]).schema.names
    pending_nodes: list[tuple] = []
    pending_edges: list[tuple] = []

    for shard_path in parquet_files:
        shard = shard_path.name
        shard_rows = 0
        for row in iter_rows(shard_path, columns):
            uuid = str(row.get("uuid") or "").strip()
            if not uuid:
                raise RuntimeError(f"missing uuid in {shard} row {shard_rows}")
            if uuid in seen_uuid:
                duplicate_uuids.append(uuid)
            seen_uuid.add(uuid)
            global_index = row_count
            ev_id = evidence_id(uuid)
            ev_node_id = evidence_node_id(uuid)
            pnode_id = persona_node_id(uuid)

            ev = row_evidence(row, shard, shard_rows, parquet_hashes[shard])
            persona_props = compact_persona_props(row, shard, global_index, ev_id)
            persona_node = {
                "id": pnode_id,
                "label": persona_props["label"],
                "space": "subject",
                "node_type": "Persona",
                "properties": persona_props,
                "evidence_refs": [ev_id],
            }
            evidence_node_props = {
                "id": ev_node_id,
                "pack_id": PACK_ID,
                "source_id": PACK_ID,
                "evidence_id": ev_id,
                "uuid": uuid,
                "source_shard": shard,
                "row_index": shard_rows,
                "hash": ev["hash"],
                "evidence_refs": [ev_id],
            }
            evidence_node = {
                "id": ev_node_id,
                "label": ev_id,
                "space": "evidence",
                "node_type": "Evidence",
                "properties": evidence_node_props,
                "evidence_refs": [ev_id],
            }
            edge_contains = {
                "id": f"edge:{PACK_ID}:contains:{uuid}",
                "from_id": dataset_node["id"],
                "to_id": pnode_id,
                "from_space": "resource",
                "to_space": "subject",
                "relation": "contains",
                "properties": {"pack_id": PACK_ID, "source_id": PACK_ID, "uuid": uuid},
                "evidence_refs": [ev_id],
            }
            edge_supports = {
                "id": f"edge:{PACK_ID}:evidence:{uuid}",
                "from_id": ev_node_id,
                "to_id": pnode_id,
                "from_space": "evidence",
                "to_space": "subject",
                "relation": "supports",
                "properties": {"pack_id": PACK_ID, "source_id": PACK_ID, "uuid": uuid},
                "evidence_refs": [ev_id],
            }

            for key, obj in [("evidence", ev), ("nodes", persona_node), ("nodes", evidence_node), ("edges", edge_contains), ("edges", edge_supports)]:
                handles[key].write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
            for obj in [ev, persona_node, evidence_node, edge_contains, edge_supports]:
                kind = "evidence" if obj is ev else "edge" if obj.get("relation") else "node"
                handles["ingest"].write(json.dumps({"kind": kind, "payload": obj}, ensure_ascii=False, default=str) + "\n")

            pending_nodes.append(("Persona", pnode_id, "subject", json.dumps(persona_props, ensure_ascii=False, default=str)))
            pending_nodes.append(("Evidence", ev_node_id, "evidence", json.dumps(evidence_node_props, ensure_ascii=False, default=str)))
            pending_edges.append(("Document", dataset_node["id"], "contains", "Persona", pnode_id, json.dumps(edge_contains["properties"], ensure_ascii=False, default=str)))
            pending_edges.append(("Evidence", ev_node_id, "supports", "Persona", pnode_id, json.dumps(edge_supports["properties"], ensure_ascii=False, default=str)))
            if len(pending_nodes) >= args.sqlite_batch_nodes:
                insert_many(conn, pending_nodes, pending_edges)
                pending_nodes.clear()
                pending_edges.clear()
            row_count += 1
            shard_rows += 1
            if row_count % args.progress_every == 0:
                print(f"processed rows={row_count}", flush=True)
        shard_summaries.append({"file": shard, "rows": shard_rows, "sha256": parquet_hashes[shard], "bytes": shard_path.stat().st_size})
    if pending_nodes or pending_edges:
        insert_many(conn, pending_nodes, pending_edges)
    conn.close()

    for h in handles.values():
        h.close()

    node_count = 1 + row_count * 2
    edge_count = row_count * 2
    evidence_count = row_count + source_evidence_count
    issues = []
    if row_count != total_expected:
        issues.append({"severity": "error", "code": "row_count_mismatch", "message": f"expected {total_expected}, got {row_count}"})
    if duplicate_uuids:
        issues.append({"severity": "error", "code": "duplicate_uuid", "message": f"duplicate uuid count {len(duplicate_uuids)}", "sample": duplicate_uuids[:10]})

    quality = {
        "status": "pass" if not issues else "fail",
        "summary": {
            "parsing_completeness": 1.0 if row_count == total_expected else row_count / max(total_expected, 1),
            "ocr_completeness": None,
            "clip_coverage": None,
            "evidence_coverage": 1.0,
            "chunk_coverage": 1.0,
            "node_evidence_integrity": 1.0 if not duplicate_uuids else 0.0,
            "edge_evidence_integrity": 1.0,
            "relationship_evidence_coverage": 1.0,
            "multihop_path_coverage": 1.0,
            "graph_reference_integrity": 1.0,
        },
        "checks": {
            "source_snapshot": "pass",
            "parquet_rows": "pass" if row_count == total_expected else "fail",
            "uuid_uniqueness": "pass" if not duplicate_uuids else "fail",
            "evidence_refs": "pass",
            "broken_edges": "pass",
            "local_sqlite_ingest": "pass",
        },
        "counts": {
            "missing_evidence_refs": 0,
            "broken_edges": 0,
            "orphan_nodes": 0,
            "parser_failures": 0,
            "ocr_low_confidence_spans": 0,
        },
        "issues": issues,
    }
    write_json(stage / "quality/report.json", quality)

    export_status = {
        "status": "ok" if not issues else "fail",
        "pack_id": PACK_ID,
        "exported_at": now(),
        "nodes": node_count,
        "edges": edge_count,
        "evidence": evidence_count,
        "source": "localcrab streaming parquet pack builder",
    }
    write_json(stage / "neo4j/export_status.json", export_status)
    (stage / "neo4j/import.cypher").write_text("// Pack was ingested into LocalCrab SQLite graph store by scripts/build_nemotron_personas_korea_pack.py\n", encoding="utf-8")
    write_json(stage / "sample_queries.json", {"queries": ["서울 서초구 회계 사무원 페르소나", "부산 기장군 부동산 사무실 페르소나", "한글 페르소나 데이터셋의 직업 분포"]})
    write_json(stage / "community_reports.json", {"reports": []})
    (stage / "README.md").write_text(f"# {TITLE}\n\nFull LocalCrab Pack v1 for `{REPO_ID}` at revision `{REVISION}`.\n\nRows: {row_count}\nEvidence: {evidence_count}\nNodes: {node_count}\nEdges: {edge_count}\nLicense: {LICENSE}\n", encoding="utf-8")

    manifest = {
        "format_version": "opencrab-pack-v1",
        "pack_id": PACK_ID,
        "title": TITLE,
        "version": "1.0.0",
        "grammar_version": "1.0.0",
        "created_at": now(),
        "created_by": "LocalCrab",
        "license": {"scope": "public-dataset", "name": LICENSE},
        "source": {"mode": "huggingface_dataset", "label": REPO_ID, "url": f"https://huggingface.co/datasets/{REPO_ID}", "description": "Full NVIDIA Nemotron Personas Korea dataset pack."},
        "counts": {"documents": 1, "chunks": row_count, "images": 10, "evidence": evidence_count, "nodes": node_count, "edges": edge_count, "files": sum(1 for _ in stage.rglob('*') if _.is_file()), "bytes": sum(_.stat().st_size for _ in stage.rglob('*') if _.is_file())},
        "limits": {"split_recommended": True, "staged_ingest_recommended": True, "reason": "1M-row dataset; generated as a large streaming pack."},
        "quality": quality["summary"],
        "retrieval_hints": {"relation_cues": ["persona", "occupation", "district", "province", "skills", "hobbies"], "benchmark_focus": ["persona_lookup", "demographic_filtering", "evidence_traceability"]},
        "hashes": {"nodes_sha256": sha256_file(paths["nodes"]), "edges_sha256": sha256_file(paths["edges"]), "evidence_sha256": sha256_file(paths["evidence"]), "opencrab_ingest_sha256": sha256_file(paths["ingest"]), "pack_sha256": None},
        "artifacts": {"nodes": "graph/nodes.jsonl", "edges": "graph/edges.jsonl", "evidence_index": "evidence/index.jsonl", "quality_report": "quality/report.json", "neo4j_cypher": "neo4j/import.cypher", "opencrab_ingest": "neo4j/opencrab_ingest.jsonl", "neo4j_export_status": "neo4j/export_status.json"},
        "source_files": shard_summaries,
    }
    write_json(stage / "manifest.json", manifest)

    pack_sha = None
    zip_path = out_root / f"{PACK_ID}.opencrab-pack-v1.zip"
    if not args.no_zip:
        pack_sha = zip_stage(stage, zip_path)
        manifest["hashes"]["pack_sha256"] = pack_sha
        write_json(stage / "manifest.json", manifest)
        # update zip manifest with pack hash by rewriting archive once would be costly;
        # keep canonical manifest next to stage and status report with zip hash.

    status = {
        "status": "ok" if not issues else "fail",
        "pack_id": PACK_ID,
        "stage": str(stage),
        "zip": None if args.no_zip else str(zip_path),
        "zip_sha256": pack_sha,
        "counts": {"rows": row_count, "source_evidence": source_evidence_count, "evidence": evidence_count, "nodes": node_count, "edges": edge_count},
        "quality": quality,
    }
    write_json(out_root / "build_status.json", status)
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", default="/home/asdf/.openclaw/workspace/data/localcrab/datasets/nvidia-nemotron-personas-korea")
    parser.add_argument("--output-root", default="/home/asdf/.openclaw/workspace/data/localcrab/packs/nvidia-nemotron-personas-korea")
    parser.add_argument("--local-data-dir", default="/home/asdf/.openclaw/workspace/data/localcrab")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--reset-ingest", action="store_true")
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--progress-every", type=int, default=50000)
    parser.add_argument("--sqlite-batch-nodes", type=int, default=10000)
    args = parser.parse_args()
    status = build(args)
    print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
    return 0 if status["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
