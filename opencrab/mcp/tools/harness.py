"""CrabHarness execution tool: apply a PromotionPackage to the ontology stores.

See graph.py's module docstring for why ``_get_context`` is imported at
function scope rather than at module level.
"""

from __future__ import annotations

import logging
from typing import Any

from ._registry import AccessTier, tool

logger = logging.getLogger(__name__)


@tool(
    "harness_promotion_apply",
    {
        "description": (
            "Apply a CrabHarness PromotionPackage to the OpenCrab ontology stores. "
            "Writes each node and edge, returning receipt_id + receipt_ts per operation. "
            "Use dry_run=true to validate grammar and schema without writing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "package": {
                    "type": "object",
                    "description": "Serialised PromotionPackage (from crabharness promotion-stub or run output).",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Validate without writing to stores.",
                    "default": False,
                },
                "pack_id": {
                    "type": "string",
                    "description": "Optional destination pack_id. Defaults to the caller's default pack. Ignored when dry_run=true.",
                },
            },
            "required": ["package"],
        },
    },
    order=15,
    # #150: ADMIN (confirmed design decision, not derived from the handler
    # body alone) -- unlike pack_create/pack_ingest, this writes an arbitrary
    # space/node_type/node_id with no owning-pack scope at all, i.e. no
    # per-pack boundary a remote principal's ownership could be checked
    # against. Grouped with schema_pack_install/uninstall as the ADMIN tier
    # (see opencrab.mcp.tools._registry.allowed_access_tiers): withheld from
    # remote (non-local) principals.
    access=AccessTier.ADMIN,
    writes=True,
)
def harness_promotion_apply(
    package: dict[str, Any],
    dry_run: bool = False,
    pack_id: str | None = None,
) -> dict[str, Any]:
    """
    Apply a CrabHarness PromotionPackage directly to the OpenCrab ontology stores.

    Accepts the promotion package as a JSON object (not a file path) so it can
    be called inline from Claude or any MCP client without file I/O.

    Each node and edge write returns a receipt_id + receipt_ts for provenance.

    Parameters
    ----------
    package:
        A serialised PromotionPackage object (from CrabHarness promotion-stub output).
    dry_run:
        If True, validate grammar + schema without writing to any store.
    pack_id:
        Optional destination pack_id. Defaults to the caller's default pack
        (``resolve_write_pack``). Not read at all when dry_run=True.

    The apply's subject is the caller's server-derived ``current_principal()``
    (#145) -- never a client argument; tenant_id stays fixed at 'default'.
    Not read at all when dry_run=True — nothing is written, so nothing is
    billed.
    """
    try:
        from crabharness.crabharness.models import PromotionPackage
    except ImportError:
        return {"error": "crabharness package not installed. Run: pip install -e crabharness/"}

    from opencrab.grammar.validator import validate_node, validate_node_properties

    try:
        promo = PromotionPackage.model_validate(package)
    except Exception as exc:
        return {"error": f"Invalid PromotionPackage: {exc}"}

    node_receipts: list[dict[str, Any]] = []
    edge_receipts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if dry_run:
        for node in promo.nodes:
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
            "package_id": promo.package_id,
            "dry_run": True,
            "node_receipts": node_receipts,
            "edge_receipts": edge_receipts,
            "errors": errors,
        }

    from opencrab.auth import current_principal
    from opencrab.mcp.tools import _get_context
    from opencrab.ontology.builder import graph_write_failed
    from opencrab.pack.ownership import (
        PackForbiddenError,
        PackNotFoundError,
        assert_writable,
        resolve_write_pack,
    )

    ctx = _get_context()
    builder = ctx["builder"]
    principal = current_principal()
    tenant_id = "default"
    subject_id = principal.user_id
    target_pack_id = resolve_write_pack(ctx["sql"], principal, pack_id)

    # Fail the whole apply up front rather than once per node/edge below --
    # every item targets the same pack_id, so a bad pack_id fails identically
    # for all of them. Same wording as pack_ingest (opencrab/mcp/tools/pack.py)
    # -- #143 invariant 7 folds "doesn't exist" and "someone else's private
    # pack" into one indistinguishable response.
    try:
        assert_writable(ctx["sql"], principal, target_pack_id)
    except PackNotFoundError:
        return {"error": "pack not found; use pack_create first", "pack_id": target_pack_id}
    except PackForbiddenError:
        # No fork tool exists yet (see pack.py's pack_ingest/pack_publish) --
        # naming one here would send callers into an unknown-tool error.
        return {
            "error": "PACK_NOT_WRITABLE: not the pack owner",
            "pack_id": target_pack_id,
            "hint": (
                "this pack is readable but not writable by you; "
                "create your own with pack_create and ingest into that"
            ),
        }

    for node in promo.nodes:
        try:
            result = builder.add_node(
                space=node.space,
                node_type=node.node_type,
                node_id=node.node_id,
                properties=node.properties or {},
                pack_id=target_pack_id,
            )
            node_receipts.append({
                "node_id": node.node_id,
                "receipt_id": result.get("receipt_id"),
                "receipt_ts": result.get("receipt_ts"),
                "stores": result.get("stores"),
            })
        except Exception as exc:
            errors.append({"node_id": node.node_id, "error": str(exc)})

    for edge in promo.edges:
        try:
            result = builder.add_edge(
                from_space=edge.from_space,
                from_id=edge.from_id,
                relation=edge.relation,
                to_space=edge.to_space,
                to_id=edge.to_id,
                pack_id=target_pack_id,
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
                "edge": f"{edge.from_id}-[{edge.relation}]->{edge.to_id}",
                "error": str(exc),
            })

    # #66 hardening: builder.add_node() never raises for a per-store failure
    # (see builder.py's module docstring) — a node_receipts entry exists even
    # when result["stores"]["graph"] is "error: ..."/"unavailable", so
    # len(node_receipts) alone overcounts. graph_write_failed() reads each
    # receipt's own "stores" map (already captured above) to bill only the
    # nodes that actually landed in the graph — the system of record.
    # Doesn't touch node_receipts/summary themselves (existing contract for
    # callers), only the count fed to billing.
    billed_node_count = sum(
        1 for r in node_receipts if not graph_write_failed(r.get("stores") or {})
    )
    if billed_node_count > 0:
        billing_result = ctx["billing"].on_harness_apply(
            tenant_id, subject_id, promo.package_id, billed_node_count
        )
        if not billing_result.get("ok"):
            # #105: don't discard emit()'s result — surface a failed persist
            # here too, without failing the (already-applied) promotion
            # package.
            logger.warning(
                "on_harness_apply billing event failed to persist (package_id=%s): %s",
                promo.package_id, billing_result.get("error"),
            )

    return {
        "package_id": promo.package_id,
        "mission_id": promo.mission_id,
        "run_id": promo.run_id,
        "dry_run": False,
        "node_receipts": node_receipts,
        "edge_receipts": edge_receipts,
        "errors": errors,
        "summary": {
            "nodes_written": len(node_receipts),
            "edges_written": len(edge_receipts),
            "errors": len(errors),
        },
    }
