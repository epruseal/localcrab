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

STORAGE (issue #105): in local/kuzu (SQLite) mode this table lives in its
OWN file, ``billing.db`` — NOT ``opencrab.db``, which ontology_nodes /
impact_records / lever_simulations / rebac_policies share. Those tables are
written under the cross-process ``write.lock``; ``opencrab.db``'s SQLAlchemy
engine does not enable WAL (see ``sql_store.py``), so it uses SQLite's
default rollback journal, which takes a whole-FILE write lock — any two
writers to the SAME file serialise, even across unrelated tables. Billing
writes are deliberately NOT covered by ``write.lock`` (ontology_query would
otherwise serialise every read behind it — see
``opencrab/mcp/tools/_registry.py``'s ``tool()`` docstring), so before this
fix a billing insert could block behind, or lose to, an unrelated long write
(e.g. a bulk ``pack_ingest``) for as long as that write took. An earlier
version of this fix added a retry-with-backoff loop around the insert; that
only shrank the failure window (and, worse, slept synchronously inside the
request path — see ``opencrab/mcp/http_app.py``'s async handler, which would
block on it). WAL would not have fixed it either: WAL only separates
readers from a writer, not writer from writer, so two writers to one file
still serialise even under WAL. The only structural fix is not sharing the
file: no query anywhere in this codebase JOINs ``billing_events`` with
another table (confirmed by grep), so there was never a reason for it to be
there. See ``opencrab.stores.factory.make_billing_sql_store`` for the store
construction and one-time migration of any pre-#105 rows. PG/docker mode is
unaffected (PostgreSQL uses row-level locking, not a whole-file lock) and
keeps billing_events on the same database as everything else.
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

    def __init__(self, sql_store: Any, *, migrate_from: Any | None = None) -> None:
        self._sql = sql_store
        self._ensure_tables()
        if migrate_from is not None:
            self._migrate_from(migrate_from)

    def _ensure_tables(self) -> None:
        try:
            ensure_tables(self._sql, _TABLES_SQLITE, _TABLES_PG)
        except Exception as exc:
            logger.warning("BillingHooks table creation failed: %s", exc)

    def _migrate_from(self, old_store: Any) -> None:
        """One-time copy of any billing_events rows left behind in
        `old_store` by a pre-#105 install (where billing shared opencrab.db
        with write.lock'd tables -- see this module's docstring).

        Cost on every call after the first successful run: one
        ``Path.exists()`` stat -- a marker file next to the target SQLite db
        short-circuits before opening either database, so a large or
        growing old table costs nothing on later startups (a naive
        ATTACH+INSERT-every-boot would re-scan the whole old table forever).

        Uses raw ``sqlite3`` rather than either store's SQLAlchemy engine:
        ATTACH/DETACH's transaction scoping is otherwise at the mercy of
        SQLAlchemy's implicit BEGIN, and a raw connection can be closed
        outright afterwards instead of returning an attached-database handle
        to a pool for some future, unrelated query to trip over.

        Deliberately does not DROP the old table (only ever meaningful in
        SQLite local/kuzu mode, and only if it's actually SQLite -- PG has
        nothing to migrate and never calls this): an operator inspecting
        opencrab.db later must not mistake it for live data, so a
        successful migration also renames it to
        ``billing_events_migrated_to_billing_db`` -- unmistakable from the
        schema itself, not just a log line that may have scrolled away.
        Actually dropping it, if ever wanted, is a separate explicit action.
        """
        import sqlite3
        from pathlib import Path

        new_url = getattr(self._sql, "_url", "")
        old_url = getattr(old_store, "_url", "")
        if not (new_url.startswith("sqlite:///") and old_url.startswith("sqlite:///")):
            return  # only meaningful for two real SQLite files

        new_db_path = Path(new_url[len("sqlite:///"):])
        old_db_path = Path(old_url[len("sqlite:///"):])
        marker = new_db_path.parent / f"{new_db_path.name}.migrated"
        if marker.exists():
            return
        if not old_db_path.is_file():
            marker.touch()  # fresh install, nothing to migrate -- stay free forever
            return

        try:
            old_conn = sqlite3.connect(str(old_db_path))
            try:
                has_table = old_conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='billing_events'"
                ).fetchone()
                if has_table:
                    new_conn = sqlite3.connect(str(new_db_path))
                    try:
                        new_conn.execute("ATTACH DATABASE ? AS old_billing", (str(old_db_path),))
                        cur = new_conn.execute(
                            "INSERT OR IGNORE INTO billing_events "
                            "(event_id, tenant_id, subject_id, event_type, count, metadata, created_at) "
                            "SELECT event_id, tenant_id, subject_id, event_type, count, metadata, created_at "
                            "FROM old_billing.billing_events"
                        )
                        moved = cur.rowcount
                        new_conn.commit()
                        new_conn.execute("DETACH DATABASE old_billing")
                    finally:
                        new_conn.close()

                    old_conn.execute(
                        "ALTER TABLE billing_events RENAME TO billing_events_migrated_to_billing_db"
                    )
                    old_conn.commit()

                    if moved > 0:
                        logger.info(
                            "billing_events: migrated %d historical row(s) from %s to %s "
                            "(issue #105). The old table in %s is renamed to "
                            "billing_events_migrated_to_billing_db -- stale, kept for safety, "
                            "no longer read or written by BillingHooks.",
                            moved, old_db_path.name, new_db_path.name, old_db_path.name,
                        )
            finally:
                old_conn.close()
        except Exception as exc:
            logger.warning(
                "billing_events migration to %s failed, will retry next startup: %s",
                new_db_path.name, exc,
            )
            return  # marker NOT written -- retry next process start

        marker.touch()

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

        try:
            with self._sql._engine.begin() as conn:
                conn.execute(text(sql), params)
        except Exception as exc:
            # issue #105: this used to retry a handful of times on "database
            # is locked" before giving up. That was papering over the real
            # problem (billing sharing a SQLite file with write.lock'd
            # writers -- see this module's docstring) with a synchronous
            # sleep in the request path, and only shrank the failure window
            # rather than closing it. Billing now lives on its own file, so
            # a transient lock here (e.g. two billing writes landing in the
            # same instant) is rare and brief enough for the DBAPI's own
            # busy-timeout (sql_store.py's explicit `timeout`) to absorb
            # without any retry loop of ours.
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
    # match the other three (wired by #66). The lock contention this pair
    # used to be exposed to is now avoided structurally (billing has its own
    # SQLite file — see this module's docstring), not retried around, so
    # there is no separate "still needs the call site's attention" case to
    # call out here anymore.

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
