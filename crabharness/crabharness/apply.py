"""
CrabHarness → OpenCrab promotion apply.

Reads a PromotionPackage JSON file and writes each node and edge into
the OpenCrab ontology stores via OntologyBuilder.

Each operation returns a receipt_id + receipt_ts (Phase 1 feature).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import PromotionPackage


def apply_promotion_package(
    package_path: str | Path,
    dry_run: bool = False,
    tenant_id: str = "default",
    subject_id: str | None = None,
) -> dict[str, Any]:
    """
    Apply a PromotionPackage to OpenCrab.

    Parameters
    ----------
    package_path:
        Path to a JSON file containing a serialised PromotionPackage.
    dry_run:
        If True, validate without writing to any store.
    tenant_id:
        Tenant identifier for the ``harness_apply`` billing event fired on a
        live (non-dry-run) apply. No CLI flag exposes this yet — the
        crabharness CLI always applies as the default tenant; multi-tenant
        CLI usage is left for a follow-up (see opencrab issue #66's PR).
    subject_id:
        Optional actor for the same billing event.

    Returns
    -------
    dict with keys:
        package_id, node_receipts, edge_receipts, errors, dry_run

    Notes
    -----
    Issue #66: this CLI/library path applies a whole PromotionPackage (many
    nodes/edges via OntologyBuilder) exactly like the MCP tool
    ``harness_promotion_apply`` — it is billed the same way, as a single
    ``harness_apply`` event whose count is the number of nodes that actually
    landed in the graph store (see ``graph_write_failed`` below: OntologyBuilder
    doesn't raise for a per-store failure, so "no exception" alone doesn't
    mean "written").
    """
    path = Path(package_path)
    if not path.exists():
        raise FileNotFoundError(f"Promotion package not found: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    package = PromotionPackage.model_validate(raw)

    # Import OpenCrab components — optional dependency
    try:
        from opencrab.config import Settings
        from opencrab.ontology.builder import OntologyBuilder, graph_write_failed
        from opencrab.stores.factory import make_doc_store, make_graph_store, make_sql_store
    except ImportError as exc:
        raise ImportError(
            "opencrab package is required for promotion apply. "
            "Install it with: pip install -e ../  (from the crabharness directory)"
        ) from exc

    settings = Settings()

    node_receipts: list[dict[str, Any]] = []
    edge_receipts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if dry_run:
        # Validate grammar + schema without writing
        from opencrab.grammar.validator import validate_node, validate_node_properties
        for node in package.nodes:
            r = validate_node(node.space, node.node_type)
            if not r.valid:
                errors.append({"node_id": node.node_id, "error": r.error})
            else:
                pr = validate_node_properties(node.node_type, node.properties or {})
                if not pr.valid:
                    errors.append({"node_id": node.node_id, "error": pr.error})
                else:
                    node_receipts.append({
                        "node_id": node.node_id,
                        "space": node.space,
                        "node_type": node.node_type,
                        "status": "dry_run_valid",
                    })
        return {
            "package_id": package.package_id,
            "node_receipts": node_receipts,
            "edge_receipts": edge_receipts,
            "errors": errors,
            "dry_run": True,
        }

    # Live write
    graph = make_graph_store(settings)
    docs = make_doc_store(settings)
    sql = make_sql_store(settings)
    builder = OntologyBuilder(neo4j=graph, mongo=docs, sql=sql)

    for node in package.nodes:
        try:
            result = builder.add_node(
                space=node.space,
                node_type=node.node_type,
                node_id=node.node_id,
                properties=node.properties or {},
            )
            node_receipts.append({
                "node_id": node.node_id,
                "space": node.space,
                "node_type": node.node_type,
                "receipt_id": result.get("receipt_id"),
                "receipt_ts": result.get("receipt_ts"),
                "stores": result.get("stores"),
            })
        except Exception as exc:
            errors.append({"node_id": node.node_id, "error": str(exc)})

    for edge in package.edges:
        try:
            result = builder.add_edge(
                from_space=edge.from_space,
                from_id=edge.from_id,
                relation=edge.relation,
                to_space=edge.to_space,
                to_id=edge.to_id,
            )
            edge_receipts.append({
                "from_id": edge.from_id,
                "relation": edge.relation,
                "to_id": edge.to_id,
                "receipt_id": result.get("receipt_id"),
                "receipt_ts": result.get("receipt_ts"),
                "stores": result.get("stores"),
            })
        except Exception as exc:
            errors.append({
                "edge": f"{edge.from_id} -[{edge.relation}]-> {edge.to_id}",
                "error": str(exc),
            })

    # #66: this path had zero billing callers (see opencrab/billing/hooks.py's
    # module docstring) — bill it the same way harness_promotion_apply (the
    # MCP twin of this function) does, counting only nodes that actually
    # landed in the graph. builder.add_node() doesn't raise for a per-store
    # failure (see builder.py's module docstring), so len(node_receipts)
    # alone would overcount — graph_write_failed() filters those out.
    try:
        from opencrab.billing.hooks import BillingHooks

        billed_node_count = sum(
            1 for r in node_receipts if not graph_write_failed(r.get("stores") or {})
        )
        if billed_node_count > 0:
            BillingHooks(sql).on_harness_apply(
                tenant_id, subject_id, package.package_id, billed_node_count
            )
    except Exception as exc:  # noqa: BLE001 — billing is fire-and-forget, never blocks apply
        import logging

        logging.getLogger(__name__).warning("harness_apply billing failed: %s", exc)

    return {
        "package_id": package.package_id,
        "mission_id": package.mission_id,
        "run_id": package.run_id,
        "node_receipts": node_receipts,
        "edge_receipts": edge_receipts,
        "errors": errors,
        "dry_run": False,
        "summary": {
            "nodes_written": len(node_receipts),
            "edges_written": len(edge_receipts),
            "errors": len(errors),
        },
    }
