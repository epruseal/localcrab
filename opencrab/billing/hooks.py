"""
Billing Hooks — event logging for usage metering.

Every billable operation fires a BillingEvent that is persisted to
`billing_events` in the SQL store. Downstream services (or a future
Stripe/Paddle integration) can read these to generate invoices.

Billable event types:
  node_write     — ontology_add_node called (successful write)
  edge_write     — ontology_add_edge called (successful write)
  query          — ontology_query or query_bm25 called
  ingest         — pack_create / pack_ingest called
  harness_apply  — harness_promotion_apply called

Each event stores: tenant_id, subject_id, event_type, count, metadata, ts.
Aggregation queries can sum counts by (tenant_id, event_type, day) for billing.

Issue #66: this module used to also document a ``promotion`` event fired by
an ``on_promotion`` hook. That hook had zero callers repo-wide *and* no tool
to call it from — the MCP tool it was meant for (``promotion_promote``) was
already deleted as dead code (see ``opencrab/mcp/tools/__init__.py``'s
module docstring; ``tests/test_mcp.py::test_tools_list_not_empty`` pins its
absence). Deleted rather than wired — there was nothing to wire it to.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from opencrab.common.timefmt import now_iso
from opencrab.execution._sql import ensure_tables, is_sqlite

logger = logging.getLogger(__name__)

_TABLES_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS billing_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id    TEXT NOT NULL UNIQUE,
        tenant_id   TEXT NOT NULL DEFAULT 'default',
        subject_id  TEXT,
        event_type  TEXT NOT NULL,
        count       INTEGER NOT NULL DEFAULT 1,
        metadata    TEXT,
        created_at  TEXT DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_billing_tenant ON billing_events (tenant_id, event_type, created_at)",
]

_TABLES_PG = [
    """
    CREATE TABLE IF NOT EXISTS billing_events (
        id          SERIAL PRIMARY KEY,
        event_id    VARCHAR(64) NOT NULL UNIQUE,
        tenant_id   VARCHAR(256) NOT NULL DEFAULT 'default',
        subject_id  VARCHAR(256),
        event_type  VARCHAR(64)  NOT NULL,
        count       INTEGER      NOT NULL DEFAULT 1,
        metadata    JSONB,
        created_at  TIMESTAMPTZ  DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_billing_tenant ON billing_events (tenant_id, event_type, created_at)",
]




def _insert_event_sql(is_sqlite: bool) -> str:
    """Return dialect-safe SQL for inserting a billing event."""
    if is_sqlite:
        return (
            "INSERT OR IGNORE INTO billing_events "
            "(event_id, tenant_id, subject_id, event_type, count, metadata) "
            "VALUES (:eid, :tid, :sid, :etype, :cnt, :meta)"
        )

    return (
        "INSERT INTO billing_events "
        "(event_id, tenant_id, subject_id, event_type, count, metadata) "
        "VALUES (:eid, :tid, :sid, :etype, :cnt, CAST(:meta AS JSONB)) "
        "ON CONFLICT (event_id) DO NOTHING"
    )


class BillingHooks:
    """
    Logs billable events to the SQL store.

    Instantiate once and pass to OntologyBuilder / tools as needed.
    All methods are fire-and-forget (errors are logged, never raised).
    """

    def __init__(self, sql_store: Any) -> None:
        self._sql = sql_store
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        try:
            ensure_tables(self._sql, _TABLES_SQLITE, _TABLES_PG)
        except Exception as exc:
            logger.warning("BillingHooks table creation failed: %s", exc)

    # ------------------------------------------------------------------
    # Core emit
    # ------------------------------------------------------------------

    def emit(
        self,
        event_type: str,
        tenant_id: str = "default",
        subject_id: str | None = None,
        count: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Persist a billing event.

        Parameters
        ----------
        event_type:
            One of: node_write, edge_write, query, ingest, promotion, harness_apply.
        tenant_id:
            Tenant identifier (default: 'default' for single-tenant deployments).
        subject_id:
            Optional actor / user performing the operation.
        count:
            Quantity (e.g. number of nodes written in a batch).
        metadata:
            Optional extra info (e.g. space, node_type, query text).

        Returns
        -------
        On success: ``{"ok": True, "event_id": ..., "event_type": ..., "tenant_id": ...,
        "count": ..., "created_at": ...}``.
        On failure (never raises): ``{"ok": False, "error": str(exc)}`` — no
        ``event_id`` is included since no row was persisted. Callers that only
        care about "did billing not crash my write" can ignore the return
        value entirely (this method never raises); callers that need to know
        whether the event was actually recorded must check ``ok``.
        """
        import json

        from sqlalchemy import text

        event_id = f"evt_{uuid.uuid4().hex[:16]}"

        meta_str: str | None = None
        if metadata:
            try:
                meta_str = json.dumps(metadata, default=str)
            except Exception:
                meta_str = str(metadata)

        try:
            sql = _insert_event_sql(is_sqlite(self._sql))
            with self._sql._engine.begin() as conn:
                conn.execute(
                    text(sql),
                    {
                        "eid": event_id,
                        "tid": tenant_id,
                        "sid": subject_id,
                        "etype": event_type,
                        "cnt": count,
                        "meta": meta_str,
                    },
                )
        except Exception as exc:
            logger.warning("BillingHooks.emit failed: %s", exc)
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "event_id": event_id,
            "event_type": event_type,
            "tenant_id": tenant_id,
            "count": count,
            "created_at": now_iso(),
        }

    # ------------------------------------------------------------------
    # Convenience wrappers (called by tools.py)
    # ------------------------------------------------------------------
    #
    # NOTE (issue #66, see also #105): on_node_write/on_query pre-date this
    # fix and discard emit()'s return value at both the wrapper and the call
    # site — a lock-contention or storage failure there is silently invisible
    # beyond a WARNING log. #105 owns fixing that pair (and emit()'s own
    # durability: retries, sqlite timeout). The three wrappers below are
    # newly wired by #66 and deliberately do NOT repeat that swallow: they
    # return emit()'s {"ok": ...} dict so their call sites can notice (and
    # log at the handler's own logger) a failed billing write instead of
    # only relying on BillingHooks' internal warning.

    def on_node_write(self, tenant_id: str, subject_id: str | None, space: str, node_type: str) -> None:
        self.emit("node_write", tenant_id, subject_id, metadata={"space": space, "node_type": node_type})

    def on_edge_write(
        self, tenant_id: str, subject_id: str | None, relation: str
    ) -> dict[str, Any]:
        return self.emit("edge_write", tenant_id, subject_id, metadata={"relation": relation})

    def on_query(self, tenant_id: str, subject_id: str | None, question: str) -> None:
        self.emit("query", tenant_id, subject_id, metadata={"question": question[:200]})

    def on_ingest(
        self, tenant_id: str, subject_id: str | None, source_id: str
    ) -> dict[str, Any]:
        return self.emit("ingest", tenant_id, subject_id, metadata={"source_id": source_id})

    def on_harness_apply(
        self, tenant_id: str, subject_id: str | None, package_id: str, node_count: int
    ) -> dict[str, Any]:
        return self.emit(
            "harness_apply", tenant_id, subject_id, count=node_count, metadata={"package_id": package_id}
        )

    # ------------------------------------------------------------------
    # Usage reporting
    # ------------------------------------------------------------------

    def get_usage(
        self,
        tenant_id: str = "default",
        event_type: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """
        Return aggregated usage counts for a tenant.

        Parameters
        ----------
        tenant_id:
            Tenant to report on.
        event_type:
            Optional filter by event type.
        since:
            Optional ISO timestamp — only count events after this time.

        Returns
        -------
        dict with 'total' and 'by_type' breakdown.
        """
        from sqlalchemy import text

        params: dict[str, Any] = {"tid": tenant_id}
        where = "WHERE tenant_id = :tid"
        if event_type:
            where += " AND event_type = :etype"
            params["etype"] = event_type
        if since:
            where += " AND created_at >= :since"
            params["since"] = since

        sql = f"SELECT event_type, SUM(count) as total FROM billing_events {where} GROUP BY event_type"

        try:
            with self._sql._engine.connect() as conn:
                rows = conn.execute(text(sql), params).fetchall()
            by_type = {r[0]: int(r[1]) for r in rows}
            return {
                "tenant_id": tenant_id,
                "total": sum(by_type.values()),
                "by_type": by_type,
            }
        except Exception as exc:
            logger.warning("BillingHooks.get_usage failed: %s", exc)
            return {"tenant_id": tenant_id, "total": 0, "by_type": {}}

    def list_events(
        self,
        tenant_id: str = "default",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return recent billing events for a tenant."""
        from sqlalchemy import text

        try:
            with self._sql._engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT event_id, tenant_id, subject_id, event_type, count, created_at "
                        "FROM billing_events WHERE tenant_id = :tid "
                        "ORDER BY id DESC LIMIT :limit"
                    ),
                    {"tid": tenant_id, "limit": limit},
                ).fetchall()
            return [dict(r._mapping) for r in rows]
        except Exception as exc:
            logger.warning("BillingHooks.list_events failed: %s", exc)
            return []
