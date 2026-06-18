#!/usr/bin/env python3
"""Streaming Neo4j -> OpenCrab ingest JSONL export for large packs."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from opencrab.common.hashing import file_sha256
from opencrab.common.neo4j_driver import make_driver
from opencrab.pack.neo4j_export import _sha_id, _stable_json

PACK_ID = "nvidia-nemotron-personas-korea"
LABELS = ["Document", "Evidence", "Persona"]
REL_TYPES = ["CONTAINS", "SUPPORTS"]
LABEL_TO_SPACE = {"Document": "resource", "Evidence": "evidence", "Persona": "subject"}

# 비트단위 동일한 opencrab 구현을 재사용한다(특성화 테스트가 동일성을 박제).
jdump = _stable_json
sha_id = _sha_id
sha256_file = file_sha256


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def clean_props(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def normalise_node(record: dict[str, Any]) -> dict[str, Any]:
    props = clean_props(record["props"])
    labels = list(record["labels"] or [])
    node_type = props.get("node_type") or next((label for label in labels if label in LABELS), labels[0] if labels else "")
    node_id = props.get("id") or sha_id("neo4j-node", props)
    return {
        "kind": "node",
        "payload": {
            "id": node_id,
            "label": props.get("label") or props.get("name") or props.get("title") or node_id,
            "space": props.get("space") or LABEL_TO_SPACE.get(str(node_type), ""),
            "node_type": node_type,
            "labels": labels,
            "properties": props,
            "evidence_refs": props.get("evidence_refs") or [],
        },
    }


def normalise_edge(record: dict[str, Any]) -> dict[str, Any]:
    rel_props = clean_props(record["rel_props"])
    source_props = clean_props(record["source_props"])
    target_props = clean_props(record["target_props"])
    relation = str(record["relation"]).lower()
    payload = {
        "from_id": source_props.get("id") or rel_props.get("from_id"),
        "to_id": target_props.get("id") or rel_props.get("to_id"),
        "from_space": source_props.get("space") or LABEL_TO_SPACE.get((record.get("source_labels") or [""])[0], ""),
        "to_space": target_props.get("space") or LABEL_TO_SPACE.get((record.get("target_labels") or [""])[0], ""),
        "relation": relation,
        "properties": rel_props,
        "confidence": rel_props.get("confidence"),
        "evidence_refs": rel_props.get("evidence_refs") or [],
        "source_labels": record.get("source_labels") or [],
        "target_labels": record.get("target_labels") or [],
    }
    payload["id"] = rel_props.get("id") or sha_id("neo4j-edge", payload)
    return {"kind": "edge", "payload": payload}


def export_nodes(session, handle, fetch_size: int) -> int:
    total = 0
    started = time.time()
    for label in LABELS:
        query = f"""
        MATCH (n:{label})
        WHERE n.pack_id = $pack_id OR n.source_id = $pack_id
        RETURN properties(n) AS props, labels(n) AS labels
        """
        result = session.run(query, pack_id=PACK_ID, fetch_size=fetch_size)
        for record in result:
            handle.write(json.dumps(normalise_node(dict(record)), ensure_ascii=False, default=str) + "\n")
            total += 1
            if total % 100000 == 0:
                print(f"exported nodes={total} elapsed={time.time()-started:.1f}s", flush=True)
    print(f"exported nodes={total} elapsed={time.time()-started:.1f}s", flush=True)
    return total


def export_edges(session, handle, fetch_size: int) -> int:
    total = 0
    started = time.time()
    for rel in REL_TYPES:
        query = f"""
        MATCH (a)-[r:{rel}]->(b)
        WHERE r.pack_id = $pack_id OR r.source_id = $pack_id
        RETURN properties(a) AS source_props,
               labels(a) AS source_labels,
               properties(b) AS target_props,
               labels(b) AS target_labels,
               properties(r) AS rel_props,
               type(r) AS relation
        """
        result = session.run(query, pack_id=PACK_ID, fetch_size=fetch_size)
        for record in result:
            handle.write(json.dumps(normalise_edge(dict(record)), ensure_ascii=False, default=str) + "\n")
            total += 1
            if total % 100000 == 0:
                print(f"exported edges={total} elapsed={time.time()-started:.1f}s", flush=True)
    print(f"exported edges={total} elapsed={time.time()-started:.1f}s", flush=True)
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/home/asdf/.openclaw/workspace/data/localcrab/packs/nvidia-nemotron-personas-korea/stage/neo4j/opencrab_ingest.neo4j.jsonl")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="opencrab")
    parser.add_argument("--fetch-size", type=int, default=2000)
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    started = time.time()
    with make_driver(GraphDatabase, args.uri, args.user, args.password, fetch_size=args.fetch_size, max_connection_lifetime=3600) as driver:
        driver.verify_connectivity()
        with driver.session() as session, output.open("w", encoding="utf-8") as handle:
            node_count = export_nodes(session, handle, args.fetch_size)
            edge_count = export_edges(session, handle, args.fetch_size)

    status = {
        "status": "ok",
        "pack_id": PACK_ID,
        "output": str(output),
        "nodes": node_count,
        "edges": edge_count,
        "lines": node_count + edge_count,
        "sha256": sha256_file(output),
        "bytes": output.stat().st_size,
        "elapsed_sec": round(time.time() - started, 2),
    }
    write_json(output.with_name("opencrab_ingest.neo4j_export_status.json"), status)
    print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
