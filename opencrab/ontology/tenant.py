"""
Tenant Context — lightweight multi-tenant isolation.

Approach (safe for early stage):
  - tenant_id is injected per-request, not stored globally
  - Nodes are stamped with tenant_id in their properties on write
  - The SQL billing_events table tracks usage per tenant
  - The doc-store audit_log (OntologyBuilder.add_node/add_edge's audit
    event, written via each doc store's log_event()) has NO tenant_id
    field in any backend (Mongo/local-JSON/SQL) — it only ever captures
    subject_id. A tenant-aware write's own audit trail cannot say which
    tenant it belonged to. This is a distinct defect from issue #119
    (subject_id reaching billing but not audit): here there is no
    tenant_id parameter on the builder or log_event() to forward in the
    first place, so it needs a schema change, not a keyword fix. Tracked
    as a follow-up, out of #119's scope.
  - No hard DB-level row isolation yet (planned for Phase 6)

TenantContext is a thin dataclass passed through the call stack.

#145: this module no longer extracts identity from client-controllable MCP
headers/arguments (``extract_tenant_context``, ``filter_by_tenant``, and
``TenantContext.allowed_spaces`` were deleted — zero production callers, and
the identity they carried is now the server-derived ``opencrab.auth``
Principal instead, see #143's "principal" definition). Only
``stamp_properties`` is still live, called from
``opencrab.mcp.tools.graph.ontology_add_node`` with a ``TenantContext`` built
from ``tenant_id="default"`` and the caller's ``current_principal().user_id``.

Usage:
    ctx = TenantContext(tenant_id="default", subject_id=current_principal().user_id)
    stamp_properties(properties, ctx)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TenantContext:
    """Carries per-request tenant and subject identity."""

    tenant_id: str = "default"
    subject_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "subject_id": self.subject_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TenantContext:
        return cls(
            tenant_id=data.get("tenant_id", "default"),
            subject_id=data.get("subject_id"),
        )

    @classmethod
    def default(cls) -> TenantContext:
        return cls(tenant_id="default")


def stamp_properties(
    properties: dict[str, Any],
    tenant_ctx: TenantContext,
) -> dict[str, Any]:
    """
    Stamp tenant_id (and optionally subject_id) into node properties.

    Preserves existing tenant_id if already set (idempotent).
    """
    stamped = {**properties}
    stamped.setdefault("tenant_id", tenant_ctx.tenant_id)
    if tenant_ctx.subject_id:
        stamped.setdefault("created_by", tenant_ctx.subject_id)
    return stamped
