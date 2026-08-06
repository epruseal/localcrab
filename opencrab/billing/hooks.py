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
  harness_apply  — harness_promotion_apply called (MCP or CLI, see below)

Each event stores: tenant_id, subject_id, event_type, count, metadata, ts.
Aggregation queries can sum counts by (tenant_id, event_type, day) for billing.

Only events for writes that actually landed are billed: `graph` is the
system of record (opencrab/ontology/builder.py's module docstring), so a
call whose write failed there is not billed even though add_node/add_edge
raise no exception for a per-store failure (see
`opencrab.ontology.builder.store_write_failures`/`graph_write_failed`).
Optional-store-only failures (docs/sql/vector) still bill — the entity
exists and is queryable, matching pack_create's own "graph failed = hard
error, optional-store-only failed = partial success" split.

A 6th event type, fired by a since-DELETED ``on_promotion`` hook, is gone
for good (issue #66: zero callers, and no code shape matches its
per-node_id signature — see git history / the PR discussion for why).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from opencrab.common.timefmt import now_iso
from opencrab.execution._sql import ensure_tables, is_sqlite

logger = logging.getLogger(__name__)

# issue #105: emit() used to make exactly one INSERT attempt and treat any
# failure (including plain lock contention from a long-running writer
# elsewhere, e.g. a bulk pack_ingest) as final. "database is locked" is the
# one sqlite3/SQLAlchemy error string that means "try again, the data is
# fine, another writer just has the file" — every other exception (schema
# error, broken engine, bad JSON, ...) means retrying is pointless. The
# needle is matched case-insensitively against str(exc); SQLAlchemy wraps
# the raw sqlite3.OperationalError but keeps its message verbatim.
_LOCK_ERROR_NEEDLE = "database is locked"
_MAX_LOCK_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 0.05


def _is_lock_error(exc: Exception) -> bool:
    return _LOCK_ERROR_NEEDLE in str(exc).lower()

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
        # issue #105: a WARNING log line was, before this fix, the ONLY trace
        # of a lost billing event -- nothing else observed it. This counter
        # is a second, queryable route: process-lifetime count of emit()
        # calls that ultimately failed to persist (lock contention that
        # outlasted the retries below, or any other error), independent of
        # whether a given caller bothered to check emit()'s return dict.
        self._emit_failures = 0
        self._ensure_tables()

    @property
    def emit_failure_count(self) -> int:
        """Count of emit() calls that failed to persist since this
        BillingHooks instance was created. See the note in __init__."""
        return self._emit_failures

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
            One of: node_write, edge_write, query, ingest, harness_apply.
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

        sql = _insert_event_sql(is_sqlite(self._sql))
        params = {
            "eid": event_id,
            "tid": tenant_id,
            "sid": subject_id,
            "etype": event_type,
            "cnt": count,
            "meta": meta_str,
        }

        attempt = 0
        while True:
            try:
                with self._sql._engine.begin() as conn:
                    conn.execute(text(sql), params)
                break
            except Exception as exc:
                if _is_lock_error(exc) and attempt < _MAX_LOCK_RETRIES:
                    attempt += 1
                    time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                    continue
                self._emit_failures += 1
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
    # NOTE (issue #66, #105): every wrapper below returns emit()'s
    # {"ok": ...} dict so its call site CAN notice (and log with its own
    # tenant/handler context) a failed billing write instead of relying only
    # on BillingHooks' internal WARNING. on_node_write/on_query used to
    # return None here, which made that structurally impossible regardless
    # of whether the call site bothered to check — #105 fixed that pair to
    # match the other three (wired by #66). emit() itself now also retries
    # on lock contention (see _is_lock_error) and counts terminal failures
    # in emit_failure_count, so a slow-but-clearing lock no longer needs the
    # call site's attention at all.

    def on_node_write(
        self, tenant_id: str, subject_id: str | None, space: str, node_type: str
    ) -> dict[str, Any]:
        return self.emit("node_write", tenant_id, subject_id, metadata={"space": space, "node_type": node_type})

    def on_edge_write(
        self, tenant_id: str, subject_id: str | None, relation: str
    ) -> dict[str, Any]:
        return self.emit("edge_write", tenant_id, subject_id, metadata={"relation": relation})

    def on_query(self, tenant_id: str, subject_id: str | None, question: str) -> dict[str, Any]:
        return self.emit("query", tenant_id, subject_id, metadata={"question": question[:200]})

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
