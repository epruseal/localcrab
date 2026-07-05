"""
PostgreSQL-backed document store — PG mode 4-store integration (doc axis).

Contract source: opencrab/stores/local_sql_doc_store.py (SQLite backend).
This module implements the same public methods with the same signatures and
return shapes: upsert_node_doc, get_node_doc, list_nodes, bm25_fingerprint,
delete_node_doc, upsert_source, keyword_search, get_source, list_sources,
log_event, get_audit_log, collection_stats, ping, close, plus the
available/supports_keyword properties.

SCHEMA: three tables mirror LocalSQLDocStore 1:1 — TEXT JSON columns become
JSONB, TEXT timestamp columns become TIMESTAMPTZ.

KEYWORD SEARCH: LocalSQLDocStore uses SQLite FTS5 + bm25(). PG has no FTS5,
so keyword_search here uses:
  - to_tsvector('simple', text) + plainto_tsquery('simple', :q) + ts_rank
    for the primary full-text leg (GIN index on to_tsvector('simple', text)).
  - a trigram ILIKE fallback (pg_trgm's similarity() + a GIN
    (text gin_trgm_ops) index) when the query contains any short token
    (< 3 chars), since to_tsquery only matches whole normalised lexemes and
    can't do substring/short-token matching the way FTS5's tokenizer can.
Score direction: "higher is better" in both branches (ts_rank / similarity
are already higher-is-better, unlike SQLite's bm25() which is
lower-is-better and gets sign-flipped in LocalSQLDocStore.keyword_search).

LIFECYCLE NOTE: close() disposes the engine only when this store created it
from a DSN string. When an external SQLAlchemy Engine is injected, the
caller owns its lifecycle and close() is a no-op (unlike
LocalSQLDocStore.close(), which always closes its own connections).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from opencrab.stores._graph_common import IDENT_RE as _SCHEMA_IDENT_RE
from opencrab.stores._graph_common import _as_dict

logger = logging.getLogger(__name__)

_DDL_TEMPLATE = [
    """
    CREATE TABLE IF NOT EXISTS {schema}.doc_nodes (
        space       TEXT NOT NULL,
        node_id     TEXT NOT NULL,
        node_type   TEXT NOT NULL DEFAULT '',
        properties  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        updated_at  TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (space, node_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_doc_nodes_updated ON {schema}.doc_nodes(updated_at)",
    """
    CREATE TABLE IF NOT EXISTS {schema}.doc_sources (
        source_id   TEXT PRIMARY KEY,
        text        TEXT NOT NULL DEFAULT '',
        metadata    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        ingested_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS {schema}.audit_log (
        event_id    TEXT PRIMARY KEY,
        event_type  TEXT NOT NULL,
        subject_id  TEXT,
        details     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        timestamp   TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON {schema}.audit_log(timestamp DESC)",
]

def _ts_str(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value is not None else ""


class PgDocStore:
    """PostgreSQL-backed document store with the same interface as LocalSQLDocStore."""

    def __init__(self, dsn_or_engine: Any, schema: str = "public") -> None:
        if not _SCHEMA_IDENT_RE.match(schema):
            raise ValueError(f"Invalid schema identifier: {schema!r}")
        self._schema = schema
        self._available = False
        self._kw_ok = False
        self._owns_engine = False

        from sqlalchemy import text
        from sqlalchemy.engine import Engine

        self._text = text

        if isinstance(dsn_or_engine, Engine):
            self._engine = dsn_or_engine
        else:
            from sqlalchemy import create_engine

            self._engine = create_engine(str(dsn_or_engine))
            self._owns_engine = True

        self._init_db()

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self, write: bool = False):
        cm = self._engine.begin() if write else self._engine.connect()
        with cm as conn:
            conn.execute(self._text("SET TIME ZONE 'UTC'"))
            yield conn

    @property
    def _t(self) -> str:
        return f'"{self._schema}"'

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError("PgDocStore is not available.")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        try:
            with self._engine.begin() as conn:
                if self._schema != "public":
                    conn.execute(self._text(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"'))
                for ddl in _DDL_TEMPLATE:
                    conn.execute(self._text(ddl.format(schema=f'"{self._schema}"')))
            self._available = True
            logger.info("PgDocStore initialised (schema=%s)", self._schema)
        except Exception as exc:
            logger.warning("PgDocStore init failed: %s", exc)
            self._available = False
            return

        # Keyword search capability: pg_trgm extension + GIN indexes.
        # Graceful-disable (supports_keyword=False) if the extension can't be
        # created (e.g. no superuser privilege) rather than raising.
        try:
            with self._engine.begin() as conn:
                conn.execute(self._text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                conn.execute(
                    self._text(
                        f"CREATE INDEX IF NOT EXISTS idx_doc_sources_fts"
                        f" ON {self._t}.doc_sources USING GIN (to_tsvector('simple', text))"
                    )
                )
                conn.execute(
                    self._text(
                        f"CREATE INDEX IF NOT EXISTS idx_doc_sources_trgm"
                        f" ON {self._t}.doc_sources USING GIN (text gin_trgm_ops)"
                    )
                )
            self._kw_ok = True
        except Exception as exc:
            logger.warning("Keyword search index unavailable (graceful): %s", exc)
            self._kw_ok = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def supports_keyword(self) -> bool:
        return self._available and self._kw_ok

    def ping(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(self._text("SELECT 1"))
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._owns_engine:
            try:
                self._engine.dispose()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Node document operations
    # ------------------------------------------------------------------

    def upsert_node_doc(
        self,
        space: str,
        node_type: str,
        node_id: str,
        properties: dict[str, Any],
    ) -> str:
        self._require_available()
        updated_at = datetime.now(UTC)
        with self._conn(write=True) as conn:
            conn.execute(
                self._text(
                    f"""
                    INSERT INTO {self._t}.doc_nodes(space, node_id, node_type, properties, updated_at)
                    VALUES (:space, :node_id, :node_type, CAST(:properties AS jsonb), :updated_at)
                    ON CONFLICT (space, node_id) DO UPDATE SET
                        node_type  = EXCLUDED.node_type,
                        properties = EXCLUDED.properties,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "space": space,
                    "node_id": node_id,
                    "node_type": node_type,
                    "properties": json.dumps(properties),
                    "updated_at": updated_at,
                },
            )
        return f"{space}::{node_id}"

    def get_node_doc(self, space: str, node_id: str) -> dict[str, Any] | None:
        self._require_available()
        with self._conn() as conn:
            row = conn.execute(
                self._text(
                    f"SELECT space, node_id, node_type, properties, updated_at"
                    f" FROM {self._t}.doc_nodes WHERE space=:space AND node_id=:node_id"
                ),
                {"space": space, "node_id": node_id},
            ).fetchone()
        if row is None:
            return None
        return self._row_to_node(row)

    def list_nodes(
        self, space: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self._require_available()
        with self._conn() as conn:
            if space:
                rows = conn.execute(
                    self._text(
                        f"SELECT space, node_id, node_type, properties, updated_at"
                        f" FROM {self._t}.doc_nodes WHERE space=:space LIMIT :lim"
                    ),
                    {"space": space, "lim": limit},
                ).fetchall()
            else:
                rows = conn.execute(
                    self._text(
                        f"SELECT space, node_id, node_type, properties, updated_at"
                        f" FROM {self._t}.doc_nodes LIMIT :lim"
                    ),
                    {"lim": limit},
                ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def bm25_fingerprint(self, limit: int = 50000) -> tuple[int, str]:
        self._require_available()
        with self._conn() as conn:
            row = conn.execute(
                self._text(
                    f"""
                    SELECT COUNT(*), MAX(updated_at)
                    FROM (SELECT updated_at FROM {self._t}.doc_nodes LIMIT :lim) sub
                    """
                ),
                {"lim": limit},
            ).fetchone()
        return (int(row[0]), _ts_str(row[1]) if row[1] is not None else "")

    def delete_node_doc(self, space: str, node_id: str) -> bool:
        self._require_available()
        with self._conn(write=True) as conn:
            result = conn.execute(
                self._text(
                    f"DELETE FROM {self._t}.doc_nodes WHERE space=:space AND node_id=:node_id"
                ),
                {"space": space, "node_id": node_id},
            )
            rowcount = result.rowcount
        return rowcount > 0

    # ------------------------------------------------------------------
    # Source ingestion
    # ------------------------------------------------------------------

    def upsert_source(
        self, source_id: str, text: str, metadata: dict[str, Any]
    ) -> str:
        self._require_available()
        ingested_at = datetime.now(UTC)
        with self._conn(write=True) as conn:
            conn.execute(
                self._text(
                    f"""
                    INSERT INTO {self._t}.doc_sources(source_id, text, metadata, ingested_at)
                    VALUES (:source_id, :text, CAST(:metadata AS jsonb), :ingested_at)
                    ON CONFLICT (source_id) DO UPDATE SET
                        text        = EXCLUDED.text,
                        metadata    = EXCLUDED.metadata,
                        ingested_at = EXCLUDED.ingested_at
                    """
                ),
                {
                    "source_id": source_id,
                    "text": text,
                    "metadata": json.dumps(metadata),
                    "ingested_at": ingested_at,
                },
            )
        return source_id

    def keyword_search(
        self,
        query: str,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not self._available or not self._kw_ok:
            return []

        toks = re.findall(r"\w+", query or "", flags=re.UNICODE)
        if not toks:
            return []

        short_token = any(len(t) < 3 for t in toks)
        overfetch = max(1, limit) * 5

        try:
            with self._conn() as conn:
                if short_token:
                    conditions = " OR ".join(f"text ILIKE :t{i}" for i in range(len(toks)))
                    params: dict[str, Any] = {
                        f"t{i}": f"%{t}%" for i, t in enumerate(toks)
                    }
                    params.update({"qraw": query, "lim": overfetch})
                    rows = conn.execute(
                        self._text(
                            f"""
                            SELECT source_id, text, metadata, similarity(text, :qraw) AS rank
                            FROM {self._t}.doc_sources
                            WHERE {conditions}
                            ORDER BY rank DESC
                            LIMIT :lim
                            """
                        ),
                        params,
                    ).fetchall()
                else:
                    rows = conn.execute(
                        self._text(
                            f"""
                            SELECT source_id, text, metadata,
                                   ts_rank(to_tsvector('simple', text), plainto_tsquery('simple', :q)) AS rank
                            FROM {self._t}.doc_sources
                            WHERE to_tsvector('simple', text) @@ plainto_tsquery('simple', :q)
                            ORDER BY rank DESC
                            LIMIT :lim
                            """
                        ),
                        {"q": query, "lim": overfetch},
                    ).fetchall()
        except Exception as exc:
            logger.warning("keyword_search failed: %s", exc)
            return []

        try:
            from opencrab.ontology.pack_provenance import matches_pack_filter
        except Exception:
            matches_pack_filter = None  # type: ignore

        out: list[dict[str, Any]] = []
        for source_id, text, meta_raw, rank in rows:
            meta = _as_dict(meta_raw)
            if matches_pack_filter is not None and not matches_pack_filter(
                {"metadata": meta}, pack_ids, include_unpackaged
            ):
                continue
            out.append({
                "source_id": source_id,
                "node_id": meta.get("node_id") or source_id,
                "text": text,
                "metadata": meta,
                "score": float(rank or 0.0),
            })
            if len(out) >= limit:
                break
        return out

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        self._require_available()
        with self._conn() as conn:
            row = conn.execute(
                self._text(
                    f"SELECT source_id, text, metadata, ingested_at"
                    f" FROM {self._t}.doc_sources WHERE source_id=:source_id"
                ),
                {"source_id": source_id},
            ).fetchone()
        if row is None:
            return None
        return {
            "source_id": row[0],
            "text": row[1],
            "metadata": _as_dict(row[2]),
            "ingested_at": _ts_str(row[3]),
        }

    def list_sources(self, limit: int = 100) -> list[dict[str, Any]]:
        self._require_available()
        with self._conn() as conn:
            rows = conn.execute(
                self._text(
                    f"SELECT source_id, text, metadata, ingested_at"
                    f" FROM {self._t}.doc_sources LIMIT :lim"
                ),
                {"lim": limit},
            ).fetchall()
        return [
            {
                "source_id": r[0],
                "text": r[1],
                "metadata": _as_dict(r[2]),
                "ingested_at": _ts_str(r[3]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def log_event(
        self,
        event_type: str,
        subject_id: str | None,
        details: dict[str, Any],
    ) -> str:
        self._require_available()
        event_id = str(uuid.uuid4())
        timestamp = datetime.now(UTC)
        with self._conn(write=True) as conn:
            conn.execute(
                self._text(
                    f"""
                    INSERT INTO {self._t}.audit_log(event_id, event_type, subject_id, details, timestamp)
                    VALUES (:event_id, :event_type, :subject_id, CAST(:details AS jsonb), :timestamp)
                    """
                ),
                {
                    "event_id": event_id,
                    "event_type": event_type,
                    "subject_id": subject_id,
                    "details": json.dumps(details),
                    "timestamp": timestamp,
                },
            )
        return event_id

    def get_audit_log(
        self, limit: int = 100, event_type: str | None = None
    ) -> list[dict[str, Any]]:
        self._require_available()
        with self._conn() as conn:
            if event_type:
                rows = conn.execute(
                    self._text(
                        f"""
                        SELECT event_id, event_type, subject_id, details, timestamp
                        FROM {self._t}.audit_log WHERE event_type=:event_type
                        ORDER BY timestamp DESC LIMIT :lim
                        """
                    ),
                    {"event_type": event_type, "lim": limit},
                ).fetchall()
            else:
                rows = conn.execute(
                    self._text(
                        f"""
                        SELECT event_id, event_type, subject_id, details, timestamp
                        FROM {self._t}.audit_log ORDER BY timestamp DESC LIMIT :lim
                        """
                    ),
                    {"lim": limit},
                ).fetchall()
        return [
            {
                "event_id": r[0],
                "event_type": r[1],
                "subject_id": r[2],
                "details": _as_dict(r[3]),
                "timestamp": _ts_str(r[4]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def collection_stats(self) -> dict[str, int]:
        self._require_available()
        counts: dict[str, int] = {}
        with self._conn() as conn:
            for table, key in [
                ("doc_nodes", "nodes"),
                ("doc_sources", "sources"),
                ("audit_log", "audit_log"),
            ]:
                row = conn.execute(
                    self._text(f"SELECT COUNT(*) FROM {self._t}.{table}")  # noqa: S608
                ).fetchone()
                counts[key] = int(row[0]) if row else 0
        return counts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_node(row: Any) -> dict[str, Any]:
        return {
            "space": row[0],
            "node_id": row[1],
            "node_type": row[2],
            "properties": _as_dict(row[3]),
            "updated_at": _ts_str(row[4]),
        }
