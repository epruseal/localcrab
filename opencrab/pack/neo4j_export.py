"""Export a verified Neo4j graph snapshot into OpenCrab Pack v1 JSONL."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _sha_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _clean_props(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _node_id(props: dict[str, Any]) -> str:
    for key in ("id", "node_id", "uuid"):
        value = props.get(key)
        if value:
            return str(value)
    return _sha_id("neo4j-node", props)


def _edge_id(payload: dict[str, Any]) -> str:
    props = _clean_props(payload.get("properties"))
    for key in ("id", "edge_id", "uuid"):
        value = props.get(key)
        if value:
            return str(value)
    return _sha_id("neo4j-edge", payload)


def _pack_filter_clause(entity: str) -> str:
    return (
        "$pack_id IS NULL "
        f"OR {entity}.pack_id = $pack_id "
        f"OR {entity}.source = $pack_id "
        f"OR {entity}.source_id = $pack_id"
    )


def _node_query(limit: int) -> str:
    return f"""
        MATCH (n)
        WHERE {_pack_filter_clause("n")}
        RETURN properties(n) AS props, labels(n) AS labels
        LIMIT {int(limit)}
    """


def _edge_query(limit: int) -> str:
    node_filter = (
        f"({_pack_filter_clause('a')}) "
        f"OR ({_pack_filter_clause('b')}) "
        f"OR ({_pack_filter_clause('r')})"
    )
    return f"""
        MATCH (a)-[r]->(b)
        WHERE {node_filter}
        RETURN properties(a) AS source_props,
               labels(a) AS source_labels,
               properties(b) AS target_props,
               labels(b) AS target_labels,
               properties(r) AS rel_props,
               type(r) AS relation
        LIMIT {int(limit)}
    """


def _normalise_node(row: dict[str, Any]) -> dict[str, Any]:
    props = _clean_props(row.get("props"))
    labels = row.get("labels") or []
    labels = labels if isinstance(labels, list) else list(labels)
    node_id = _node_id(props)
    return {
        "kind": "node",
        "payload": {
            "id": node_id,
            "label": props.get("label") or props.get("name") or props.get("title") or node_id,
            "space": props.get("space") or props.get("ontology_space") or "",
            "node_type": props.get("node_type") or props.get("type") or (labels[0] if labels else ""),
            "labels": labels,
            "properties": props,
            "evidence_refs": props.get("evidence_refs") or props.get("evidence_ids") or [],
        },
    }


def _normalise_edge(row: dict[str, Any]) -> dict[str, Any]:
    source_props = _clean_props(row.get("source_props"))
    target_props = _clean_props(row.get("target_props"))
    rel_props = _clean_props(row.get("rel_props"))
    source_id = _node_id(source_props)
    target_id = _node_id(target_props)
    payload = {
        "from_id": source_id,
        "to_id": target_id,
        "from_space": source_props.get("space") or source_props.get("ontology_space") or "",
        "to_space": target_props.get("space") or target_props.get("ontology_space") or "",
        "relation": str(row.get("relation") or rel_props.get("relation") or "").lower(),
        "properties": rel_props,
        "confidence": rel_props.get("confidence"),
        "evidence_refs": rel_props.get("evidence_refs") or rel_props.get("evidence_ids") or [],
        "source_labels": row.get("source_labels") or [],
        "target_labels": row.get("target_labels") or [],
    }
    payload["id"] = _edge_id(payload)
    return {"kind": "edge", "payload": payload}


def export_neo4j_opencrab_ingest(
    neo4j_store: Any,
    output_path: str | Path,
    *,
    pack_id: str | None = None,
    node_limit: int = 500_000,
    edge_limit: int = 1_000_000,
) -> dict[str, Any]:
    """Export Neo4j's loaded graph into OpenCrab Pack v1 ingest JSONL.

    The output is meant to be stored at `neo4j/opencrab_ingest.jsonl` inside a
    pack. It is derived from Neo4j after import/check, so the pack contains the
    actual graph state that passed graph verification.
    """
    if not getattr(neo4j_store, "available", False):
        raise RuntimeError("Neo4j store is not available.")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    params = {"pack_id": pack_id}

    node_rows = neo4j_store.run_cypher(_node_query(node_limit), params)
    edge_rows = neo4j_store.run_cypher(_edge_query(edge_limit), params)

    node_count = 0
    edge_count = 0
    with output.open("w", encoding="utf-8") as handle:
        for row in node_rows:
            handle.write(_stable_json(_normalise_node(row)) + "\n")
            node_count += 1
        for row in edge_rows:
            handle.write(_stable_json(_normalise_edge(row)) + "\n")
            edge_count += 1

    status = {
        "status": "ok",
        "pack_id": pack_id,
        "output": str(output),
        "nodes": node_count,
        "edges": edge_count,
        "exported_at": datetime.now(UTC).isoformat(),
    }
    output.with_name("export_status.json").write_text(_stable_json(status) + "\n", encoding="utf-8")
    return status
