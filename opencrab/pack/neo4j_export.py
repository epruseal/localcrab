"""Export a verified Neo4j graph snapshot into OpenCrab Pack v1 JSONL."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Canonical id/serialisation helpers live in opencrab.common.ids now; these
# aliases keep the historical module-private names (and their importers) working
# with byte-identical output.
from opencrab.common.ids import canonical_json as _stable_json
from opencrab.common.ids import stable_id as _sha_id

# Named presets for the strict/copy/rel_endpoint_fallback trio below. These are
# additive sugar on top of the individual flags -- they do NOT replace them.
# scripts/export_pack_graph_from_neo4j.py and tests/test_structural_characterization.py
# call _normalise_node/_normalise_edge directly with the individual flags (in
# specific combinations, including asserting the strict=True KeyError path), so
# the flags themselves stay exactly as-is; `preset` just supplies their
# defaults when the caller doesn't pass them explicitly (explicit kwargs always
# win over the preset).
_NODE_PRESETS: dict[str, dict[str, bool]] = {
    "lenient": {"strict": False, "copy": False},
    "strict": {"strict": True, "copy": True},
}
_EDGE_PRESETS: dict[str, dict[str, bool]] = {
    "lenient": {"strict": False, "copy": False, "rel_endpoint_fallback": False},
    "strict": {"strict": True, "copy": True, "rel_endpoint_fallback": True},
}


def _clean_props(value: Any, *, copy: bool = False) -> dict[str, Any]:
    """Coerce ``value`` to a dict.

    ``copy=False`` (default) returns the *same* object — the historical
    opencrab behaviour. ``copy=True`` returns a shallow copy (what the export
    script did, to avoid mutating the source row).
    """
    if not isinstance(value, dict):
        return {}
    return dict(value) if copy else value


def _node_id(props: dict[str, Any]) -> str:
    for key in ("id", "node_id", "uuid"):
        value = props.get(key)
        if value:
            return str(value)
    return _sha_id("neo4j-node", props)


def _resolve_node_type(
    props: dict[str, Any],
    labels: list[Any],
    *,
    label_priority: list[str] | None = None,
) -> str:
    explicit = props.get("node_type") or props.get("type")
    if explicit:
        return explicit
    if label_priority:
        for label in labels:
            if label in label_priority:
                return label
    return labels[0] if labels else ""


def _resolve_space(
    props: dict[str, Any],
    label_key: Any,
    *,
    label_to_space: dict[str, str] | None = None,
) -> str:
    space = props.get("space") or props.get("ontology_space")
    if space:
        return space
    if label_to_space:
        return label_to_space.get(str(label_key), "")
    return ""


def _endpoint_id(
    props: dict[str, Any],
    rel_props: dict[str, Any],
    rel_key: str,
    *,
    rel_endpoint_fallback: bool = False,
) -> str:
    for key in ("id", "node_id", "uuid"):
        value = props.get(key)
        if value:
            return str(value)
    if rel_endpoint_fallback:
        value = rel_props.get(rel_key)
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


def _normalise_node(
    row: dict[str, Any],
    *,
    label_to_space: dict[str, str] | None = None,
    label_priority: list[str] | None = None,
    preset: str | None = None,
    strict: bool | None = None,
    copy: bool | None = None,
) -> dict[str, Any]:
    """Normalise a Neo4j node row into OpenCrab Pack v1 shape.

    Defaults reproduce opencrab's historical behaviour (graceful ``.get`` access,
    no label-based space inference, ``node_type=labels[0]``). The export script
    opts into its domain rules via params:

    * ``label_to_space``  — infer ``space`` from a label when props lack one.
    * ``label_priority``  — pick ``node_type`` by label priority over ``labels[0]``.
    * ``strict``          — require ``props``/``labels`` keys (KeyError if absent).
    * ``copy``            — shallow-copy props instead of sharing the source dict.
    * ``preset``          — ``"lenient"`` (default) or ``"strict"``, supplies the
      ``strict``/``copy`` defaults above; an explicitly-passed ``strict``/``copy``
      always overrides the preset.

    The ``ontology_space`` / ``props["type"]`` / ``evidence_ids`` fallbacks are
    always applied (the script previously lacked them — this is a widening, not a
    weakening: it only adds fallbacks, never drops a constraint).
    """
    defaults = _NODE_PRESETS.get(preset, _NODE_PRESETS["lenient"])
    strict = defaults["strict"] if strict is None else strict
    copy = defaults["copy"] if copy is None else copy
    props = _clean_props(row["props"] if strict else row.get("props"), copy=copy)
    labels = list((row["labels"] if strict else row.get("labels")) or [])
    node_type = _resolve_node_type(props, labels, label_priority=label_priority)
    node_id = _node_id(props)
    return {
        "kind": "node",
        "payload": {
            "id": node_id,
            "label": props.get("label") or props.get("name") or props.get("title") or node_id,
            "space": _resolve_space(props, node_type, label_to_space=label_to_space),
            "node_type": node_type,
            "labels": labels,
            "properties": props,
            "evidence_refs": props.get("evidence_refs") or props.get("evidence_ids") or [],
        },
    }


def _normalise_edge(
    row: dict[str, Any],
    *,
    label_to_space: dict[str, str] | None = None,
    preset: str | None = None,
    strict: bool | None = None,
    copy: bool | None = None,
    rel_endpoint_fallback: bool | None = None,
) -> dict[str, Any]:
    """Normalise a Neo4j edge row. See :func:`_normalise_node` for the param
    semantics (including ``preset``); ``rel_endpoint_fallback`` additionally
    lets ``from_id``/``to_id`` fall back to ``rel_props['from_id'/'to_id']``
    (the script's behaviour) before hashing."""
    defaults = _EDGE_PRESETS.get(preset, _EDGE_PRESETS["lenient"])
    strict = defaults["strict"] if strict is None else strict
    copy = defaults["copy"] if copy is None else copy
    rel_endpoint_fallback = defaults["rel_endpoint_fallback"] if rel_endpoint_fallback is None else rel_endpoint_fallback
    source_props = _clean_props(row["source_props"] if strict else row.get("source_props"), copy=copy)
    target_props = _clean_props(row["target_props"] if strict else row.get("target_props"), copy=copy)
    rel_props = _clean_props(row["rel_props"] if strict else row.get("rel_props"), copy=copy)
    source_labels = row.get("source_labels") or []
    target_labels = row.get("target_labels") or []
    relation_raw = row["relation"] if strict else row.get("relation")
    payload = {
        "from_id": _endpoint_id(source_props, rel_props, "from_id", rel_endpoint_fallback=rel_endpoint_fallback),
        "to_id": _endpoint_id(target_props, rel_props, "to_id", rel_endpoint_fallback=rel_endpoint_fallback),
        "from_space": _resolve_space(source_props, source_labels[0] if source_labels else "", label_to_space=label_to_space),
        "to_space": _resolve_space(target_props, target_labels[0] if target_labels else "", label_to_space=label_to_space),
        "relation": str(relation_raw or rel_props.get("relation") or "").lower(),
        "properties": rel_props,
        "confidence": rel_props.get("confidence"),
        "evidence_refs": rel_props.get("evidence_refs") or rel_props.get("evidence_ids") or [],
        "source_labels": source_labels,
        "target_labels": target_labels,
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

    # All four backends implement export_nodes()/export_edges() natively —
    # Neo4jStore's Cypher for both is copied verbatim from this module's
    # former _node_query()/_edge_query() helpers (now removed), so this call
    # produces byte-identical rows to the hand-rolled run_cypher() calls it
    # replaces. See opencrab/stores/_graph_protocol.py.
    node_rows = neo4j_store.export_nodes(pack_id, node_limit)
    edge_rows = neo4j_store.export_edges(pack_id, edge_limit)

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
