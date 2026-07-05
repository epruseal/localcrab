"""
PostgreSQL-backed document store — PG mode 4-store integration (doc axis).

Contract source: opencrab/stores/local_sql_doc_store.py (SQLite backend).
This module implements the same public methods with the same signatures and
return shapes: upsert_node_doc, get_node_doc, list_nodes, bm25_fingerprint,
delete_node_doc, upsert_source, keyword_search, get_source, list_sources,
log_event, get_audit_log, collection_stats, ping, close, plus the
available/supports_keyword properties.

STAGE 6a F2: the 13-method surface (everything but keyword_search) is
inherited from ``_SqlDocStoreBase`` (opencrab/stores/_sql_doc_base.py),
parameterised by the POSTGRES ``SqlDialect`` (opencrab/stores/_sql_dialect.py).
This module owns connection/engine management, DDL bootstrap, lifecycle, and
keyword_search — see _sql_doc_base.py's module docstring for the adoption
contract.

SCHEMA: three tables mirror LocalSQLDocStore 1:1 — TEXT JSON columns become
JSONB, TEXT timestamp columns become TIMESTAMPTZ. DDL is rendered from the
shared ``DOC_STORE_SCHEMA`` spec via ``POSTGRES.render_ddl(...)``.

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

import logging
import re
from contextlib import contextmanager
from typing import Any

from opencrab.stores._graph_common import IDENT_RE as _SCHEMA_IDENT_RE
from opencrab.stores._graph_common import _as_dict
from opencrab.stores._sql_dialect import POSTGRES
from opencrab.stores._sql_doc_base import DOC_STORE_SCHEMA, _SqlDocStoreBase

logger = logging.getLogger(__name__)


class PgDocStore(_SqlDocStoreBase):
    """PostgreSQL-backed document store with the same interface as LocalSQLDocStore."""

    _dialect = POSTGRES

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

    # ------------------------------------------------------------------
    # _SqlDocStoreBase hooks
    # ------------------------------------------------------------------

    def _table(self, name: str) -> str:
        return f'"{self._schema}".{name}'

    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[Any]:
        with self._conn() as conn:
            return conn.execute(self._text(sql), params).fetchall()

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> Any | None:
        with self._conn() as conn:
            return conn.execute(self._text(sql), params).fetchone()

    def _exec_write(self, sql: str, params: dict[str, Any]) -> int:
        with self._conn(write=True) as conn:
            return conn.execute(self._text(sql), params).rowcount

    def _row_get(self, row: Any, name: str) -> Any:
        return row._mapping[name]

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError(f"{type(self).__name__} is not available.")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        try:
            with self._engine.begin() as conn:
                if self._schema != "public":
                    conn.execute(self._text(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"'))
                for ddl in POSTGRES.render_ddl(DOC_STORE_SCHEMA, schema_name=self._schema):
                    conn.execute(self._text(ddl))
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
                        f" ON {self._table('doc_sources')} USING GIN (to_tsvector('simple', text))"
                    )
                )
                conn.execute(
                    self._text(
                        f"CREATE INDEX IF NOT EXISTS idx_doc_sources_trgm"
                        f" ON {self._table('doc_sources')} USING GIN (text gin_trgm_ops)"
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
    # Keyword search — PG-specific (tsvector/ts_rank + pg_trgm ILIKE
    # fallback); not provided by _SqlDocStoreBase, see its class docstring.
    # ------------------------------------------------------------------

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
        table = self._table("doc_sources")

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
                            FROM {table}
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
                            FROM {table}
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
