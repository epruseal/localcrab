"""
_SqlDocStoreBase — shared implementation of the 13-method doc-store surface
(upsert_node_doc, get_node_doc, list_nodes, bm25_fingerprint, delete_node_doc,
upsert_source, keyword_search, get_source, list_sources, log_event,
get_audit_log, collection_stats, ping), parameterised by a ``SqlDialect``
(SQLITE or POSTGRES from ``_sql_dialect.py``).

STAGE 6a STATUS: this base is authored and unit-tested standalone. It is NOT
yet wired into LocalSQLDocStore / PgDocStore — that migration is Stage 6a's
F1 (SQLite) / F2 (PG) adopter follow-up, done as separate, individually
reviewable changes given how risky this refactor class is. factory.py, the
two stores' public class names, and their module paths are all unchanged by
this file's existence.

ADOPTION CONTRACT — a subclass must:
  1. Set ``self._dialect = SQLITE`` or ``POSTGRES`` before any base method
     runs (typically the first line of ``__init__``).
  2. Implement the low-level hooks below — this is where connection/engine
     management genuinely differs (SQLite: thread-local sqlite3 connection
     via ``_SqliteConnMixin``; PG: short-lived SQLAlchemy engine
     connections via a ``with self._engine.connect()/.begin()`` context):
       - ``_table(name) -> str``            table-name qualification
         (SQLite: ``name`` as-is; PG: ``f'"{self._schema}".{name}'``)
       - ``_fetch_all(sql, params) -> list[RowLike]``
       - ``_fetch_one(sql, params) -> RowLike | None``
       - ``_exec_write(sql, params) -> int``     rowcount; must commit
       - ``_row_get(row, name) -> Any``          name-based column access
         (sqlite3.Row supports ``row[name]`` natively; a SQLAlchemy Core
         Row needs ``row._mapping[name]``)
       - ``_require_available() -> None``        raise if store unavailable
  3. Override ``keyword_search`` entirely — FTS5 bm25() (SQLite) vs
     tsvector/ts_rank + pg_trgm ILIKE fallback (PG) are too divergent for a
     shared fragment (see the two stores' module docstrings). This base
     does not implement it at all (no default, no abstractmethod stub
     needed beyond the class not defining it).
  4. Own DDL bootstrap and keyword-search capability probing (FTS5 virtual
     table / pg_trgm extension) in their own ``_init_db``, calling
     ``self._dialect.render_ddl(DOC_STORE_SCHEMA, schema_name=...)`` for the
     three core tables' CREATE TABLE / CREATE INDEX statements.
  5. Provide ``available`` / ``supports_keyword`` properties and
     ``ping()`` / ``close()`` — lifecycle stays store-specific because it's
     entangled with each backend's connection model.
  6. SQLite specifically: ``upsert_source`` must additionally sync the FTS5
     shadow table (delete+insert) after calling ``super().upsert_source()``
     — this base's ``upsert_source`` only touches ``doc_sources``, matching
     what PG needs verbatim (PG has no denormalized keyword index table to
     sync; it queries ``to_tsvector(text)`` live at search time).

NOT covered here, by design: connection/engine setup, keyword_search,
lifecycle (ping/close/available/supports_keyword), DDL bootstrap sequencing.
Covered here: the 13 methods' SQL text and dict-shaping logic.
"""

from __future__ import annotations

import abc
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from opencrab.stores._graph_common import _as_dict
from opencrab.stores._sql_dialect import Column, IndexSpec, SchemaSpec, SqlDialect, TableSpec

# ---------------------------------------------------------------------------
# One dialect-neutral schema spec for the three doc-store tables. Column-by-
# column checked against LocalSQLDocStore._DDL and pg_doc_store._DDL_TEMPLATE
# at authoring time (Stage 6a).
# ---------------------------------------------------------------------------

DOC_STORE_SCHEMA = SchemaSpec(
    tables=(
        TableSpec(
            name="doc_nodes",
            columns=(
                Column("space", "text"),
                Column("node_id", "text"),
                Column("node_type", "text", default=""),
                Column("properties", "json", default="{}"),
                Column("updated_at", "timestamp"),
            ),
            primary_key=("space", "node_id"),
        ),
        TableSpec(
            name="doc_sources",
            columns=(
                Column("source_id", "text"),
                Column("text", "text", default=""),
                Column("metadata", "json", default="{}"),
                Column("ingested_at", "timestamp"),
            ),
            primary_key=("source_id",),
        ),
        TableSpec(
            name="audit_log",
            columns=(
                Column("event_id", "text"),
                Column("event_type", "text"),
                Column("subject_id", "text", not_null=False),
                Column("details", "json", default="{}"),
                Column("timestamp", "timestamp"),
            ),
            primary_key=("event_id",),
        ),
    ),
    indexes=(
        IndexSpec("idx_doc_nodes_updated", "doc_nodes", "updated_at"),
        IndexSpec("idx_audit_ts", "audit_log", "timestamp DESC"),
    ),
)


def _ts_str(value: Any) -> str:
    """Duplicate of pg_doc_store._ts_str (duck-typed: works for the ISO
    string SQLite already stores as well as the datetime object PG returns).
    Kept local rather than imported from pg_doc_store.py to avoid a reverse
    import from this not-yet-wired base back into a store module; de-dupe
    when F2 wires PgDocStore onto this base."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value is not None else ""


class _SqlDocStoreBase(abc.ABC):
    _dialect: SqlDialect

    # ------------------------------------------------------------------
    # Hooks subclasses must implement
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def _table(self, name: str) -> str: ...

    @abc.abstractmethod
    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[Any]: ...

    @abc.abstractmethod
    def _fetch_one(self, sql: str, params: dict[str, Any]) -> Any | None: ...

    @abc.abstractmethod
    def _exec_write(self, sql: str, params: dict[str, Any]) -> int: ...

    @abc.abstractmethod
    def _row_get(self, row: Any, name: str) -> Any: ...

    @abc.abstractmethod
    def _require_available(self) -> None: ...

    # ------------------------------------------------------------------
    # Node document operations
    # ------------------------------------------------------------------

    def upsert_node_doc(
        self, space: str, node_type: str, node_id: str, properties: dict[str, Any]
    ) -> str:
        self._require_available()
        now = datetime.now(UTC)
        sql = self._dialect.upsert(
            self._table("doc_nodes"),
            ["space", "node_id", "node_type", "properties", "updated_at"],
            conflict_cols=["space", "node_id"],
            update_cols=["node_type", "properties", "updated_at"],
            json_columns=["properties"],
        )
        self._exec_write(
            sql,
            {
                "space": space,
                "node_id": node_id,
                "node_type": node_type,
                "properties": json.dumps(properties),
                "updated_at": self._dialect.bind_value_for_timestamp(now),
            },
        )
        return f"{space}::{node_id}"

    def get_node_doc(self, space: str, node_id: str) -> dict[str, Any] | None:
        self._require_available()
        sql = (
            f"SELECT space, node_id, node_type, properties, updated_at"
            f" FROM {self._table('doc_nodes')} WHERE space=:space AND node_id=:node_id"
        )
        row = self._fetch_one(sql, {"space": space, "node_id": node_id})
        if row is None:
            return None
        return self._row_to_node(row)

    def list_nodes(self, space: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        self._require_available()
        table = self._table("doc_nodes")
        # ORDER BY updated_at DESC: 정렬 없이 LIMIT 만 걸면 어느 행이 뽑힐지 SQL 표준상
        # 보장되지 않는다(#63). 최신순으로 고정해 상한을 넘는 코퍼스에서도 최소한
        # 최근 변경분은 검색 가능하고, 선택 결과가 결정적이도록 한다.
        if space:
            sql = (
                f"SELECT space, node_id, node_type, properties, updated_at"
                f" FROM {table} WHERE space=:space ORDER BY updated_at DESC LIMIT :lim"
            )
            params = {"space": space, "lim": limit}
        else:
            sql = (
                f"SELECT space, node_id, node_type, properties, updated_at"
                f" FROM {table} ORDER BY updated_at DESC LIMIT :lim"
            )
            params = {"lim": limit}
        rows = self._fetch_all(sql, params)
        return [self._row_to_node(r) for r in rows]

    def bm25_fingerprint(self, limit: int = 50000) -> tuple[int, str]:
        """Cheap ``(COUNT(*), MAX(updated_at))`` staleness probe over the WHOLE
        ``doc_nodes`` table — deliberately independent of ``limit`` (#63).

        The BM25 index only ever holds up to ``limit`` rows (see
        ``HybridQuery`` / ``_BM25_NODE_LIMIT``), but the fingerprint must not
        share that cap: once the corpus exceeds it, a capped COUNT pins at
        exactly ``limit`` forever, so count-based change detection would never
        fire again regardless of row ordering. ``limit`` is kept as a
        parameter only for call-site compatibility with callers that pass the
        BM25 cap; it is not applied here.
        """
        self._require_available()
        sql = (
            f"SELECT COUNT(*) AS cnt, MAX(updated_at) AS max_ts"
            f" FROM {self._table('doc_nodes')}"
        )
        row = self._fetch_one(sql, {})
        count = int(self._row_get(row, "cnt"))
        max_ts = self._row_get(row, "max_ts")
        return (count, _ts_str(max_ts) if max_ts is not None else "")

    def delete_node_doc(self, space: str, node_id: str) -> bool:
        self._require_available()
        sql = f"DELETE FROM {self._table('doc_nodes')} WHERE space=:space AND node_id=:node_id"
        rowcount = self._exec_write(sql, {"space": space, "node_id": node_id})
        return rowcount > 0

    # ------------------------------------------------------------------
    # Source ingestion
    # ------------------------------------------------------------------

    def upsert_source(self, source_id: str, text: str, metadata: dict[str, Any]) -> str:
        """Writes doc_sources only. SQLite subclasses must additionally sync
        the FTS5 shadow table after calling this (see class docstring)."""
        self._require_available()
        now = datetime.now(UTC)
        sql = self._dialect.upsert(
            self._table("doc_sources"),
            ["source_id", "text", "metadata", "ingested_at"],
            conflict_cols=["source_id"],
            update_cols=["text", "metadata", "ingested_at"],
            json_columns=["metadata"],
        )
        self._exec_write(
            sql,
            {
                "source_id": source_id,
                "text": text,
                "metadata": json.dumps(metadata),
                "ingested_at": self._dialect.bind_value_for_timestamp(now),
            },
        )
        return source_id

    # keyword_search: NOT implemented here — see class docstring (#3).

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        self._require_available()
        sql = (
            f"SELECT source_id, text, metadata, ingested_at"
            f" FROM {self._table('doc_sources')} WHERE source_id=:source_id"
        )
        row = self._fetch_one(sql, {"source_id": source_id})
        if row is None:
            return None
        return self._row_to_source(row)

    def list_sources(self, limit: int = 100) -> list[dict[str, Any]]:
        self._require_available()
        sql = (
            f"SELECT source_id, text, metadata, ingested_at"
            f" FROM {self._table('doc_sources')} LIMIT :lim"
        )
        rows = self._fetch_all(sql, {"lim": limit})
        return [self._row_to_source(r) for r in rows]

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def log_event(
        self, event_type: str, subject_id: str | None, details: dict[str, Any]
    ) -> str:
        self._require_available()
        event_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        sql = self._dialect.insert(
            self._table("audit_log"),
            ["event_id", "event_type", "subject_id", "details", "timestamp"],
            json_columns=["details"],
        )
        self._exec_write(
            sql,
            {
                "event_id": event_id,
                "event_type": event_type,
                "subject_id": subject_id,
                "details": json.dumps(details),
                "timestamp": self._dialect.bind_value_for_timestamp(now),
            },
        )
        return event_id

    def get_audit_log(
        self, limit: int = 100, event_type: str | None = None
    ) -> list[dict[str, Any]]:
        self._require_available()
        table = self._table("audit_log")
        if event_type:
            sql = (
                f"SELECT event_id, event_type, subject_id, details, timestamp"
                f" FROM {table} WHERE event_type=:event_type"
                f" ORDER BY timestamp DESC LIMIT :lim"
            )
            params = {"event_type": event_type, "lim": limit}
        else:
            sql = (
                f"SELECT event_id, event_type, subject_id, details, timestamp"
                f" FROM {table} ORDER BY timestamp DESC LIMIT :lim"
            )
            params = {"lim": limit}
        rows = self._fetch_all(sql, params)
        return [self._row_to_audit(r) for r in rows]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    _STATS_TABLES = (
        ("doc_nodes", "nodes"),
        ("doc_sources", "sources"),
        ("audit_log", "audit_log"),
    )

    def collection_stats(self) -> dict[str, int]:
        self._require_available()
        counts: dict[str, int] = {}
        for table, key in self._STATS_TABLES:
            sql = f"SELECT COUNT(*) AS cnt FROM {self._table(table)}"  # noqa: S608
            row = self._fetch_one(sql, {})
            counts[key] = int(self._row_get(row, "cnt")) if row is not None else 0
        return counts

    def ping(self) -> bool:
        try:
            self._fetch_one("SELECT 1 AS ok", {})
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Row -> dict shaping
    #
    # _as_dict() (opencrab.stores._graph_common) tolerates both a raw JSON
    # TEXT string (what sqlite3 returns for the JSON-typed columns here) and
    # an already-decoded dict/None (what psycopg2 returns for JSONB) — so
    # this shaping code needs no per-dialect branch, unlike
    # LocalSQLDocStore (json.loads) vs PgDocStore (_as_dict) today.
    # ------------------------------------------------------------------

    def _row_to_node(self, row: Any) -> dict[str, Any]:
        return {
            "space": self._row_get(row, "space"),
            "node_id": self._row_get(row, "node_id"),
            "node_type": self._row_get(row, "node_type"),
            "properties": _as_dict(self._row_get(row, "properties")),
            "updated_at": _ts_str(self._row_get(row, "updated_at")),
        }

    def _row_to_source(self, row: Any) -> dict[str, Any]:
        return {
            "source_id": self._row_get(row, "source_id"),
            "text": self._row_get(row, "text"),
            "metadata": _as_dict(self._row_get(row, "metadata")),
            "ingested_at": _ts_str(self._row_get(row, "ingested_at")),
        }

    def _row_to_audit(self, row: Any) -> dict[str, Any]:
        return {
            "event_id": self._row_get(row, "event_id"),
            "event_type": self._row_get(row, "event_type"),
            "subject_id": self._row_get(row, "subject_id"),
            "details": _as_dict(self._row_get(row, "details")),
            "timestamp": _ts_str(self._row_get(row, "timestamp")),
        }
