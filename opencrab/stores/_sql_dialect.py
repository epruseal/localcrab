"""
SqlDialect — a small, frozen value object capturing the SQLite ↔ PostgreSQL
SQL-text differences exercised by the doc-store 13-method surface
(local_sql_doc_store.py / pg_doc_store.py).

THIS IS NOT A GENERAL SQL RENDERER. It only reproduces the handful of
fragments those two stores actually emit today:
    - INSERT (plain) / INSERT-as-upsert (``INSERT ... ON CONFLICT (...) DO
      UPDATE SET ...`` — identical shape on both dialects; SQLite has
      supported this upsert syntax since 3.24 (2018), see "ROWID STABILITY"
      below for why this replaced an earlier ``INSERT OR REPLACE`` SQLite
      branch)
    - DDL for the three doc-store tables (doc_nodes / doc_sources /
      audit_log), from one dialect-neutral ``SchemaSpec``
    - a couple of small per-value helpers (timestamp bind value, "now"
      SQL fragment, json_extract-vs-->> for a future graph-store user)

WHAT IT DELIBERATELY DOES NOT COVER:
    - keyword_search: FTS5 bm25() vs tsvector/pg_trgm is too divergent for a
      shared fragment (per Stage 6a instructions) — each store keeps its own
      full implementation; see local_sql_doc_store.py / pg_doc_store.py.
    - schema/table qualification (PG's `"schema".table` prefix): that is
      store-owned state (``self._schema``), not a dialect concern — callers
      pass already-qualified table names into ``insert``/``upsert``.
    - placeholder binding at the call site: both engines accept NAMED
      (``:name``) bind params in practice (sqlite3 supports paramstyle
      "named" natively, not just "qmark" — see sqlite3 docs), so
      ``_sql_doc_base.py`` uses ``:name`` uniformly for both dialects. This
      dialect still records ``placeholder_style`` because it's an accurate
      *characterization* of what the pre-refactor hand-written SQL in
      local_sql_doc_store.py (qmark ``?``) and pg_doc_store.py (named
      ``:x``) emits today — useful for tests/docs, not because the shared
      base needs two code paths for it.

DERIVATION: every fragment below was checked against the literal SQL text in
local_sql_doc_store.py and pg_doc_store.py at Stage 6a authoring time (see
DOC_STORE_SCHEMA in _sql_doc_base.py for the exact column-by-column mapping).

ROWID STABILITY (Stage 6b post-adoption fix — a real regression the reviewer
caught, not a Stage 6a design choice): ``upsert()``'s SQLite branch originally
emitted ``INSERT OR REPLACE``, which is a DELETE-then-INSERT under the hood —
it allocates a NEW rowid for the replaced row. The pre-refactor
local_graph_store.py instead hand-wrote ``INSERT ... ON CONFLICT (...) DO
UPDATE SET ...`` (rowid-preserving), because _sql_graph_base.py's
find_neighbors()/export_*() rely on stable, repeatable no-ORDER-BY scan order
across re-upserts of already-seen keys (a LIMIT-capped hub fan-out must
return the *same* truncated subset run over run, not just an unordered
equivalent set — see _sql_graph_base.py's find_neighbors() docstring). Once
LocalGraphStore adopted this shared dialect, the OR-REPLACE branch silently
reintroduced that instability. Fixed by dropping the SQLite/PG branch
entirely: SQLite has supported ``ON CONFLICT (...) DO UPDATE SET col =
EXCLUDED.col`` since 3.24 (2018), so one code path now serves both dialects.
The doc-store callers (upsert_node_doc/upsert_source) are unaffected by this
change in the sense that matters here — list_nodes()/list_sources() are
already exercised only via order-agnostic (sorted-set) parity assertions, so
they never depended on OR REPLACE's delete+reinsert behavior for
correctness — but they get the same rowid-preserving upsert as a side effect
of the unification.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

ColumnKind = Literal["text", "json", "timestamp"]


@dataclass(frozen=True)
class Column:
    """One DDL column. ``default`` is an abstract literal ("" or "{}"); it is
    rendered per-dialect by ``SqlDialect.render_ddl`` (e.g. "{}" becomes
    ``'{}'`` for SQLite TEXT but ``'{}'::jsonb`` for PG JSONB)."""

    name: str
    kind: ColumnKind
    not_null: bool = True
    default: str | None = None


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]


@dataclass(frozen=True)
class IndexSpec:
    name: str
    table: str
    expr: str = ""  # e.g. "updated_at" or "timestamp DESC"
    json_key: tuple[str, str] | None = None
    """(column, key) for a JSON-field expression index (e.g. graph-store's
    idx_nodes_pack on properties->>'pack_id'). Mutually exclusive with
    ``expr`` — when set, ``SqlDialect.render_ddl`` computes the per-dialect
    expression via ``json_index_expr`` instead of using ``expr`` verbatim,
    since a JSON-key extraction is not renderable as one dialect-neutral
    string (see ``SqlDialect.json_get`` / ``json_index_expr``)."""


@dataclass(frozen=True)
class SchemaSpec:
    tables: tuple[TableSpec, ...]
    indexes: tuple[IndexSpec, ...]


@dataclass(frozen=True)
class SqlDialect:
    name: Literal["sqlite", "postgres"]
    placeholder_style: Literal["qmark", "named"]

    # ------------------------------------------------------------------
    # Value shaping (Python-side bind values / read-side decode)
    # ------------------------------------------------------------------

    def now_expr(self) -> str:
        """SQL-side "current time" fragment.

        NOTE: neither doc store's hand-written SQL actually calls this today
        — both compute ``datetime.now(UTC)`` in Python so both backends
        share one wall-clock source of truth instead of trusting the DB
        server's clock (which could differ from the app server's). Provided
        for DDL ``DEFAULT`` clauses and for graph-store dialect reuse.
        """
        return "datetime('now')" if self.name == "sqlite" else "NOW()"

    def bind_value_for_timestamp(self, dt: datetime) -> Any:
        """Python value to bind for a ``timestamp`` column, matching what
        each store's current code passes: SQLite binds an ISO-8601 string
        (TEXT column), PG binds a ``datetime`` object directly (TIMESTAMPTZ,
        coerced by the driver)."""
        return dt.isoformat() if self.name == "sqlite" else dt

    def json_get(self, col: str, key: str) -> str:
        """Per-key JSON field extraction. Not exercised by the doc-store 13
        methods (they round-trip whole JSON blobs) — provided for the
        graph-store dialect reuse (``list_packs()``'s
        ``json_extract(properties, '$.pack_id')`` vs ``properties->>'pack_id'``
        pattern), which is Stage 6b scope, not this stage's stores."""
        if self.name == "sqlite":
            return f"json_extract({col}, '$.{key}')"
        return f"{col}->>'{key}'"

    def json_index_expr(self, col: str, key: str) -> str:
        """``json_get`` wrapped for use as a functional-index expression.

        PostgreSQL requires an extra pair of parens around an *operator*
        expression (``->>``) used in ``CREATE INDEX ... (expr)`` — a bare
        function call (SQLite's ``json_extract(...)``) needs none. Matches
        pg_graph_store.py's ``idx_nodes_pack ON graph_nodes((properties->>
        'pack_id'))`` vs local_graph_store.py's ``idx_nodes_pack ON
        graph_nodes(json_extract(properties, '$.pack_id'))`` verbatim.
        """
        expr = self.json_get(col, key)
        return f"({expr})" if self.name == "postgres" else expr

    # ------------------------------------------------------------------
    # INSERT / UPSERT
    # ------------------------------------------------------------------

    def _value_exprs(self, columns: Sequence[str], json_columns: Sequence[str]) -> list[str]:
        """Both dialects render NAMED (``:col``) placeholders here — sqlite3
        accepts paramstyle "named" natively (see module docstring), so using
        it uniformly lets ``_sql_doc_base.py`` pass one params dict to either
        backend's execute call. Only PG's JSON columns get the ``CAST(...
        AS jsonb)`` wrapper; SQLite has no such cast (JSON is stored as
        plain TEXT)."""
        return [
            f"CAST(:{c} AS jsonb)" if (c in json_columns and self.name == "postgres") else f":{c}"
            for c in columns
        ]

    def insert(
        self,
        table: str,
        columns: Sequence[str],
        *,
        json_columns: Sequence[str] = (),
    ) -> str:
        """Plain INSERT (no conflict handling) — e.g. audit_log's log_event,
        which always writes a fresh uuid4 PK and never conflicts."""
        col_list = ", ".join(columns)
        values = ", ".join(self._value_exprs(columns, json_columns))
        return f"INSERT INTO {table}({col_list})\nVALUES ({values})"

    def upsert(
        self,
        table: str,
        columns: Sequence[str],
        conflict_cols: Sequence[str],
        update_cols: Sequence[str],
        *,
        json_columns: Sequence[str] = (),
    ) -> str:
        """INSERT ... ON CONFLICT (...) DO UPDATE SET ... — identical shape on
        both dialects (see module docstring's "ROWID STABILITY": SQLite's
        upsert syntax, supported since 3.24, is used here instead of INSERT
        OR REPLACE precisely because it preserves the existing row's rowid
        instead of deleting and reinserting it).
        """
        base = self.insert(table, columns, json_columns=json_columns)
        conflict = ", ".join(conflict_cols)
        set_clause = ",\n    ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        return f"{base}\nON CONFLICT ({conflict}) DO UPDATE SET\n    {set_clause}"

    # ------------------------------------------------------------------
    # DDL
    # ------------------------------------------------------------------

    def _column_type(self, kind: ColumnKind) -> str:
        if kind == "json":
            return "JSONB" if self.name == "postgres" else "TEXT"
        if kind == "timestamp":
            return "TIMESTAMPTZ" if self.name == "postgres" else "TEXT"
        return "TEXT"

    def _default_literal(self, default: str) -> str:
        if default == "{}":
            return "'{}'::jsonb" if self.name == "postgres" else "'{}'"
        return f"'{default}'"

    def render_ddl(self, schema: SchemaSpec, *, schema_name: str | None = None) -> list[str]:
        """One schema spec -> this dialect's DDL statement list, in the same
        order LocalSQLDocStore._DDL / pg_doc_store._DDL_TEMPLATE emit them
        (CREATE TABLE, then that table's indexes, table by table).

        ``schema_name`` is only meaningful for PG (qualifies each statement
        with ``"schema".table``); SQLite has no schema concept here and
        ignores it (mirrors LocalSQLDocStore, which never qualifies table
        names).
        """
        prefix = f'"{schema_name}".' if (self.name == "postgres" and schema_name) else ""
        stmts: list[str] = []
        for table in schema.tables:
            col_lines: list[str] = []
            single_pk = table.primary_key[0] if len(table.primary_key) == 1 else None
            for col in table.columns:
                sql_type = self._column_type(col.kind)
                line = f"{col.name} {sql_type}"
                if col.name == single_pk:
                    line += " PRIMARY KEY"
                else:
                    if col.not_null:
                        line += " NOT NULL"
                    if col.default is not None:
                        line += f" DEFAULT {self._default_literal(col.default)}"
                col_lines.append(line)
            if single_pk is None and table.primary_key:
                col_lines.append(f"PRIMARY KEY ({', '.join(table.primary_key)})")
            body = ",\n        ".join(col_lines)
            stmts.append(f"CREATE TABLE IF NOT EXISTS {prefix}{table.name} (\n        {body}\n    )")
            for idx in schema.indexes:
                if idx.table == table.name:
                    expr = self.json_index_expr(*idx.json_key) if idx.json_key else idx.expr
                    stmts.append(
                        f"CREATE INDEX IF NOT EXISTS {idx.name} ON {prefix}{idx.table}({expr})"
                    )
        return stmts


SQLITE = SqlDialect(name="sqlite", placeholder_style="qmark")
POSTGRES = SqlDialect(name="postgres", placeholder_style="named")
