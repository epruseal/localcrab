#!/usr/bin/env python3
"""Import a staged OpenCrab Pack v1 graph into Neo4j.

Streams graph/nodes.jsonl and graph/edges.jsonl in bounded batches. Intended for
large LocalCrab packs where JSONL is already materialized and evidence refs must
be preserved on every graph object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from opencrab.common.graph_identity import (
    GraphWriteCapabilityUnavailable,
    canonical_edge_digest,
    canonical_json_bytes,
    normalize_edge_properties,
)
from opencrab.common.graph_identity import (
    prepare_node as prepare_graph_node,
)
from opencrab.common.pack_tags import apply_pack_tag
from opencrab.stores.neo4j_store import Neo4jStore

PACK_ID = "nvidia-nemotron-personas-korea"
SPACE_TO_LABEL = {
    "resource": "Document",
    "subject": "Persona",
    "evidence": "Evidence",
}
VALID_NODE_LABELS = {"Document", "Persona", "Evidence"}
VALID_REL_TYPES = {"CONTAINS", "SUPPORTS"}


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def chunks(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def cypher_label(value: str) -> str:
    if value not in VALID_NODE_LABELS:
        raise ValueError(f"unsupported label: {value}")
    return value


def cypher_rel(value: str) -> str:
    value = value.upper()
    if value not in VALID_REL_TYPES:
        raise ValueError(f"unsupported relationship type: {value}")
    return value


def prepare_node(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    label = row.get("node_type") or SPACE_TO_LABEL.get(row.get("space"), "")
    label = cypher_label(str(label))
    props = dict(row.get("properties") or {})
    props.update({
        "id": row["id"],
        "label": row.get("label"),
        "space": row.get("space"),
        "pack_id": props.get("pack_id") or PACK_ID,
        "source_id": props.get("source_id") or PACK_ID,
        "evidence_refs": row.get("evidence_refs") or props.get("evidence_refs") or [],
    })
    # pack_id is synthesised here from the pack file's own properties, so an input
    # carrying the retired `pack` alias would land a row where the two disagree
    # (#171). Drop it -- pack_id above is authoritative for this import.
    apply_pack_tag(props, props["pack_id"])
    _node_type, props, space_id, digest = prepare_graph_node(label, row["id"], props, row.get("space"))
    return label, {"id": row["id"], "props": props, "node_type": label, "space_id": space_id, "node_digest": digest}


def prepare_edge(row: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    rel = cypher_rel(str(row.get("relation") or ""))
    from_label = cypher_label(SPACE_TO_LABEL.get(row.get("from_space"), ""))
    to_label = cypher_label(SPACE_TO_LABEL.get(row.get("to_space"), ""))
    props = dict(row.get("properties") or {})
    props.update({
        "id": row.get("id"),
        "pack_id": props.get("pack_id") or PACK_ID,
        "source_id": props.get("source_id") or PACK_ID,
        "relation": row.get("relation"),
        "from_id": row.get("from_id"),
        "to_id": row.get("to_id"),
        "evidence_refs": row.get("evidence_refs") or props.get("evidence_refs") or [],
    })
    apply_pack_tag(props, props["pack_id"])          # see prepare_node
    props = normalize_edge_properties(row["from_id"], rel, row["to_id"], props)
    edge_key = hashlib.sha256(canonical_json_bytes([row["from_id"], rel, row["to_id"]])).hexdigest()
    digest = canonical_edge_digest(row["from_id"], rel, row["to_id"], from_label, to_label, props)
    return from_label, rel, to_label, {
        "from_id": row["from_id"], "to_id": row["to_id"], "props": props,
        "from_type": from_label, "to_type": to_label,
        "edge_key": edge_key, "edge_digest": digest,
    }


def ensure_schema(store: Neo4jStore) -> None:
    store.ensure_constraints()


def reset_pack(store: Neo4jStore) -> None:
    """Reset is an administrative fixture operation, not an importer write."""
    raise GraphWriteCapabilityUnavailable(
        "pack reset requires an explicitly disposable fixture facade"
    )


def import_nodes(store: Neo4jStore, nodes_path: Path, batch_size: int) -> int:
    total = 0
    pending: list[dict[str, Any]] = []
    started = time.time()

    for row in iter_jsonl(nodes_path):
        label, payload = prepare_node(row)
        pending.append({"node_type": label, "node_id": payload["id"], "properties": payload["props"], "space_id": payload["space_id"]})
        if len(pending) >= batch_size:
            total += int(store.upsert_nodes_batch(pending))
            pending = []
    if pending:
        total += int(store.upsert_nodes_batch(pending))
    print(f"imported nodes={total} elapsed={time.time()-started:.1f}s", flush=True)
    return total


def import_edges(store: Neo4jStore, edges_path: Path, batch_size: int) -> int:
    total = 0
    pending: list[dict[str, Any]] = []
    started = time.time()

    for row in iter_jsonl(edges_path):
        from_label, rel, to_label, payload = prepare_edge(row)
        pending.append({"from_type": from_label, "from_id": payload["from_id"], "relation": rel, "to_type": to_label, "to_id": payload["to_id"], "properties": payload["props"]})
        if len(pending) >= batch_size:
            total += int(store.upsert_edges_batch(pending))
            pending = []
    if pending:
        total += int(store.upsert_edges_batch(pending))
    print(f"imported edges={total} elapsed={time.time()-started:.1f}s", flush=True)
    return total



def hydrate_evidence(store: Neo4jStore, evidence_path: Path, batch_size: int) -> int:
    """Attach full evidence text/hash/source metadata to Evidence nodes."""
    total = 0
    pending: list[dict[str, Any]] = []
    started = time.time()

    for obj in iter_jsonl(evidence_path):
        if obj.get("kind") != "persona_row":
            continue
        source = obj.get("source") or {}
        parser = obj.get("parser") or {}
        uuid = str(source.get("uuid") or obj.get("row", {}).get("uuid") or "")
        if not uuid:
            continue
        hydrated_props = {
            "id": f"evidence-node:{uuid}",
            "evidence_id": obj.get("evidence_id"),
            "hash": obj.get("hash"),
            "text": obj.get("text") or "",
            "source_path": source.get("path"),
            "source_url": source.get("url"),
            "source_title": source.get("title"),
            "source_file_sha256": source.get("file_sha256"),
            "parser_status": parser.get("status"),
            "parser_method": parser.get("method"),
            "row_index": source.get("row_index"),
            "uuid": uuid,
            "pack_id": PACK_ID,
            "source_id": PACK_ID,
        }
        pending.append({"node_id": f"evidence-node:{uuid}", "properties": hydrated_props})
        if len(pending) >= batch_size:
            total += int(store.hydrate_evidence(pending))
            pending = []
    if pending:
        total += int(store.hydrate_evidence(pending))
    print(f"hydrated evidence={total} elapsed={time.time()-started:.1f}s", flush=True)
    return total


def validate(store: Neo4jStore) -> dict[str, Any]:
    return store.validate_import(PACK_ID)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", default="/home/asdf/.openclaw/workspace/data/localcrab/packs/nvidia-nemotron-personas-korea/stage")
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default="opencrab")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--hydrate-evidence", action="store_true", help="Hydrate Evidence nodes from evidence/index.jsonl after graph import.")
    parser.add_argument("--hydrate-only", action="store_true", help="Only hydrate/validate; do not import graph nodes/edges.")
    args = parser.parse_args()

    stage = Path(args.stage)
    nodes_path = stage / "graph/nodes.jsonl"
    edges_path = stage / "graph/edges.jsonl"
    evidence_path = stage / "evidence/index.jsonl"
    status_path = stage.parent / "neo4j_import_status.json"

    store = Neo4jStore(args.uri, args.user, args.password, database=None)
    try:
        ensure_schema(store)
        if args.reset and not args.validate_only and not args.hydrate_only:
            print("reset requested; an explicitly disposable fixture facade is required", flush=True)
            reset_pack(store)
        node_count = edge_count = evidence_hydrated = None
        if not args.validate_only and not args.hydrate_only:
            node_count = import_nodes(store, nodes_path, args.batch_size)
            edge_count = import_edges(store, edges_path, args.batch_size)
        if args.hydrate_evidence or args.hydrate_only:
            evidence_hydrated = hydrate_evidence(store, evidence_path, args.batch_size)
        validation = validate(store)
    finally:
        store.close()

    status = {
        "status": "ok",
        "pack_id": PACK_ID,
        "imported_nodes": node_count,
        "imported_edges": edge_count,
        "hydrated_evidence": evidence_hydrated,
        "validation": validation,
        "stage": str(stage),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
