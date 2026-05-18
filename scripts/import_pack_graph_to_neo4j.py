#!/usr/bin/env python3
"""Import a staged OpenCrab Pack v1 graph into Neo4j.

Streams graph/nodes.jsonl and graph/edges.jsonl in bounded batches. Intended for
large LocalCrab packs where JSONL is already materialized and evidence refs must
be preserved on every graph object.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from neo4j import GraphDatabase

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
        "node_type": row.get("node_type"),
        "pack_id": props.get("pack_id") or PACK_ID,
        "source_id": props.get("source_id") or PACK_ID,
        "evidence_refs": row.get("evidence_refs") or props.get("evidence_refs") or [],
    })
    return label, {"id": row["id"], "props": props}


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
    return from_label, rel, to_label, {"from_id": row["from_id"], "to_id": row["to_id"], "props": props}


def ensure_schema(session) -> None:
    for label in sorted(VALID_NODE_LABELS):
        session.run(f"CREATE CONSTRAINT localcrab_{label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE").consume()
    session.run("CREATE INDEX localcrab_pack_id IF NOT EXISTS FOR (n:Document) ON (n.pack_id)").consume()
    session.run("CREATE INDEX localcrab_persona_pack_id IF NOT EXISTS FOR (n:Persona) ON (n.pack_id)").consume()
    session.run("CREATE INDEX localcrab_evidence_pack_id IF NOT EXISTS FOR (n:Evidence) ON (n.pack_id)").consume()


def reset_pack(session) -> None:
    # Relationship delete first, then nodes. Scoped by pack_id/source_id.
    session.run(
        """
        MATCH ()-[r]->()
        WHERE r.pack_id = $pack_id OR r.source_id = $pack_id
        DELETE r
        """,
        pack_id=PACK_ID,
    ).consume()
    session.run(
        """
        MATCH (n)
        WHERE n.pack_id = $pack_id OR n.source_id = $pack_id
        DETACH DELETE n
        """,
        pack_id=PACK_ID,
    ).consume()


def import_nodes(session, nodes_path: Path, batch_size: int) -> int:
    total = 0
    pending: dict[str, list[dict[str, Any]]] = defaultdict(list)
    started = time.time()

    def flush(label: str) -> None:
        nonlocal total
        rows = pending[label]
        if not rows:
            return
        query = f"""
        UNWIND $rows AS row
        MERGE (n:{label} {{id: row.id}})
        SET n += row.props
        """
        session.run(query, rows=rows).consume()
        total += len(rows)
        pending[label] = []
        if total % 100000 == 0:
            print(f"imported nodes={total} elapsed={time.time()-started:.1f}s", flush=True)

    for row in iter_jsonl(nodes_path):
        label, payload = prepare_node(row)
        pending[label].append(payload)
        if len(pending[label]) >= batch_size:
            flush(label)
    for label in list(pending):
        flush(label)
    print(f"imported nodes={total} elapsed={time.time()-started:.1f}s", flush=True)
    return total


def import_edges(session, edges_path: Path, batch_size: int) -> int:
    total = 0
    pending: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    started = time.time()

    def flush(key: tuple[str, str, str]) -> None:
        nonlocal total
        rows = pending[key]
        if not rows:
            return
        from_label, rel, to_label = key
        query = f"""
        UNWIND $rows AS row
        MATCH (a:{from_label} {{id: row.from_id}})
        MATCH (b:{to_label} {{id: row.to_id}})
        MERGE (a)-[r:{rel}]->(b)
        SET r += row.props
        """
        session.run(query, rows=rows).consume()
        total += len(rows)
        pending[key] = []
        if total % 100000 == 0:
            print(f"imported edges={total} elapsed={time.time()-started:.1f}s", flush=True)

    for row in iter_jsonl(edges_path):
        from_label, rel, to_label, payload = prepare_edge(row)
        key = (from_label, rel, to_label)
        pending[key].append(payload)
        if len(pending[key]) >= batch_size:
            flush(key)
    for key in list(pending):
        flush(key)
    print(f"imported edges={total} elapsed={time.time()-started:.1f}s", flush=True)
    return total



def hydrate_evidence(session, evidence_path: Path, batch_size: int) -> int:
    """Attach full evidence text/hash/source metadata to Evidence nodes."""
    total = 0
    pending: list[dict[str, Any]] = []
    started = time.time()

    def flush() -> None:
        nonlocal total, pending
        if not pending:
            return
        session.run(
            """
            UNWIND $rows AS row
            MATCH (e:Evidence {id: row.node_id})
            SET e.evidence_id = row.evidence_id,
                e.hash = row.hash,
                e.text = row.text,
                e.source_path = row.source_path,
                e.source_url = row.source_url,
                e.source_title = row.source_title,
                e.source_file_sha256 = row.source_file_sha256,
                e.parser_status = row.parser_status,
                e.parser_method = row.parser_method,
                e.row_index = row.row_index,
                e.uuid = row.uuid,
                e.pack_id = $pack_id,
                e.source_id = $pack_id
            """,
            rows=pending,
            pack_id=PACK_ID,
        ).consume()
        total += len(pending)
        pending = []
        if total % 100000 == 0:
            print(f"hydrated evidence={total} elapsed={time.time()-started:.1f}s", flush=True)

    for obj in iter_jsonl(evidence_path):
        if obj.get("kind") != "persona_row":
            continue
        source = obj.get("source") or {}
        parser = obj.get("parser") or {}
        uuid = str(source.get("uuid") or obj.get("row", {}).get("uuid") or "")
        if not uuid:
            continue
        pending.append({
            "node_id": f"evidence-node:{uuid}",
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
        })
        if len(pending) >= batch_size:
            flush()
    flush()
    print(f"hydrated evidence={total} elapsed={time.time()-started:.1f}s", flush=True)
    return total


def validate(session) -> dict[str, Any]:
    result: dict[str, Any] = {}
    result["nodes_by_label"] = session.run(
        """
        MATCH (n)
        WHERE n.pack_id = $pack_id OR n.source_id = $pack_id
        RETURN labels(n)[0] AS label, count(n) AS count
        ORDER BY label
        """,
        pack_id=PACK_ID,
    ).data()
    result["edges_by_type"] = session.run(
        """
        MATCH ()-[r]->()
        WHERE r.pack_id = $pack_id OR r.source_id = $pack_id
        RETURN type(r) AS type, count(r) AS count
        ORDER BY type
        """,
        pack_id=PACK_ID,
    ).data()
    result["missing_node_evidence_refs"] = session.run(
        """
        MATCH (n)
        WHERE (n.pack_id = $pack_id OR n.source_id = $pack_id)
          AND (n:Persona OR n:Evidence)
          AND (n.evidence_refs IS NULL OR size(n.evidence_refs) = 0)
        RETURN count(n) AS count
        """,
        pack_id=PACK_ID,
    ).single()["count"]
    result["missing_edge_evidence_refs"] = session.run(
        """
        MATCH ()-[r]->()
        WHERE (r.pack_id = $pack_id OR r.source_id = $pack_id)
          AND (r.evidence_refs IS NULL OR size(r.evidence_refs) = 0)
        RETURN count(r) AS count
        """,
        pack_id=PACK_ID,
    ).single()["count"]
    result["unhydrated_evidence_nodes"] = session.run(
        """
        MATCH (e:Evidence)
        WHERE (e.pack_id = $pack_id OR e.source_id = $pack_id)
          AND (e.text IS NULL OR e.hash IS NULL OR e.source_path IS NULL)
        RETURN count(e) AS count
        """,
        pack_id=PACK_ID,
    ).single()["count"]
    result["sample"] = session.run(
        """
        MATCH (d:Document {id: $dataset_id})-[c:CONTAINS]->(p:Persona)<-[s:SUPPORTS]-(e:Evidence)
        RETURN p.id AS persona_id, p.evidence_refs AS persona_evidence_refs,
               e.id AS evidence_node_id, c.evidence_refs AS contains_evidence_refs,
               s.evidence_refs AS supports_evidence_refs
        LIMIT 1
        """,
        dataset_id=f"dataset:{PACK_ID}",
    ).data()
    return result


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

    with GraphDatabase.driver(args.uri, auth=(args.user, args.password), max_connection_lifetime=3600) as driver:
        driver.verify_connectivity()
        with driver.session() as session:
            ensure_schema(session)
            if args.reset and not args.validate_only and not args.hydrate_only:
                print("resetting existing pack graph in Neo4j", flush=True)
                reset_pack(session)
            node_count = edge_count = evidence_hydrated = None
            if not args.validate_only and not args.hydrate_only:
                node_count = import_nodes(session, nodes_path, args.batch_size)
                edge_count = import_edges(session, edges_path, args.batch_size)
            if args.hydrate_evidence or args.hydrate_only:
                evidence_hydrated = hydrate_evidence(session, evidence_path, args.batch_size)
            validation = validate(session)

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
