"""
SqlDialect — a small, frozen value object capturing the SQLite ↔ PostgreSQL
SQL-text differences that this codebase's dual-dialect modules share.

WHO USES IT is deliberately not enumerated here. The set has grown steadily
since this object was introduced, and every list written down went stale on
the next adopter. Ask the code instead:

    grep -rn "_sql_dialect import\\|self\\._dialect\\." --include="*.py" opencrab/

THIS IS NOT A GENERAL SQL RENDERER. It only reproduces the handful of
fragments its callers actually need:
    - INSERT (plain) / INSERT-as-upsert (``INSERT ... ON CONFLICT (...) DO
      UPDATE SET ...`` — identical shape on both dialects; SQLite has
      supported this upsert syntax since 3.24 (2018), see "ROWID STABILITY"
      below for why this replaced an earlier ``INSERT OR REPLACE`` SQLite
      branch)
    - DDL rendered from one dialect-neutral ``SchemaSpec``
    - a couple of small per-value helpers (timestamp bind value, "now"
      SQL fragment, json_extract-vs-->> for per-key JSON access)

WHAT IT DELIBERATELY DOES NOT COVER:
    - keyword_search: FTS5 bm25() vs tsvector/pg_trgm is too divergent for a
      shared fragment (per Stage 6a instructions) — each doc store keeps
      its own full implementation.
    - schema/table qualification (PG's `"schema".table` prefix): that is
      store-owned state (``self._schema``), not a dialect concern — callers
      pass already-qualified table names into ``insert``/``upsert``.
    - placeholder binding at the call site: both engines accept NAMED
      (``:name``) bind params in practice (sqlite3 supports paramstyle
      "named" natively, not just "qmark" — see sqlite3 docs), so shared
      call sites can use ``:name`` uniformly for both dialects. This
      dialect still records ``placeholder_style`` as an accurate
      *characterization* of the two dialects' conventional styles (SQLite
      qmark ``?``, PG named ``:x``) — useful for tests/docs, not because
      any shared code path branches on it.

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

import json
from collections.abc import Callable, Sequence
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

        Two ways to stamp a timestamp coexist in this codebase, and they are
        not interchangeable. Computing ``datetime.now(UTC)`` in Python gives
        both backends one wall-clock source of truth instead of trusting the
        DB server's clock (which could differ from the app server's); using
        this fragment puts the clock in the statement, which is what a SET
        clause needs when there is no Python-side value to bind (e.g. an
        upsert whose ``updated_at`` must follow the row's DDL DEFAULT
        format). Also used for DDL ``DEFAULT`` clauses.
        """
        return "datetime('now')" if self.name == "sqlite" else "NOW()"

    def bind_value_for_timestamp(self, dt: datetime) -> Any:
        """Python value to bind for a ``timestamp`` column: SQLite binds an
        ISO-8601 string (its timestamp columns are TEXT), PG binds a
        ``datetime`` object directly (TIMESTAMPTZ, coerced by the driver)."""
        return dt.isoformat() if self.name == "sqlite" else dt

    def json_get(self, col: str, key: str) -> str:
        """Per-key JSON field extraction, e.g.
        ``json_extract(properties, '$.pack_id')`` vs
        ``properties->>'pack_id'``.

        This is for reading ONE key inside SQL — filter and scope predicates
        that must run in the WHERE clause. It is unrelated to round-tripping
        a whole JSON column, which callers do in Python with ``json.loads``;
        a store that round-trips blobs in Python still reaches for this the
        moment it needs to filter on a key."""
        if self.name == "sqlite":
            return f"json_extract({col}, '$.{key}')"
        return f"{col}->>'{key}'"

    def json_truthy_text(self, col: str, key: str) -> str:
        """Canonical TEXT form of a JSON field, or SQL NULL — mirroring
        Python's truthiness test in ``opencrab/stores/_graph_common.py``'s
        ``_node_pack_id`` (``str(pid) if pid else None``), not just
        ``json_get``'s bare extraction:

        - JSON ``null``/missing key -> NULL (``json_get`` already got this
          part right on its own).
        - JSON ``""`` (empty string) -> NULL (``json_get`` alone does NOT:
          a bare extraction is non-NULL text ``''``, which a naive ``pid IS
          NULL`` check would miss, wrongly treating an empty-string pack_id
          as a real foreign pack_id instead of "no pack_id").
        - JSON ``0``/``0.0``/``false`` -> NULL (same gap: these are non-NULL
          but Python-falsy). Zero is compared numerically (PG: cast to
          ``numeric``; SQLite: ``json_extract`` already normalises any zero
          REAL to ``'0.0'`` on CAST), not as raw text, so ``0``/``0.0``/
          ``0.00``/``-0`` all collapse to the same NULL regardless of how
          the number was originally written.
        - JSON string ``"0"`` -> ``'0'`` (stays truthy — distinguished from
          the number ``0`` via ``json_type``/``jsonb_typeof``, since a bare
          text comparison cannot tell a JSON number from a JSON string that
          happens to look like one).
        - JSON number (e.g. ``1``) -> its TEXT form (e.g. ``'1'``), so it
          can actually match a ``pack_ids`` list of strings — SQLite's
          ``json_extract`` preserves the JSON scalar's native type, and
          comparing an INTEGER to a bound TEXT parameter never matches.
        - JSON ``true`` -> ``'True'`` (matches Python's ``str(True)``).

        SCALAR VALUES ONLY. This is exact for string/number/boolean/null —
        every shape ``pack_id`` has ever actually held (measured live,
        2026-08-05: 252,579/252,585 graph_nodes and 614,986/614,988
        graph_edges rows are JSON text, the rest missing; zero object,
        array, or number/boolean rows). For a JSON object or array,
        behavior is UNDEFINED relative to Python: this falls through to
        ``ELSE`` (SQLite: the raw JSON text, e.g. ``'{"a":1}'``; PG: same
        via ``->>``), while Python's ``str(pid)`` on a dict/list produces
        Python's own repr (e.g. ``"{'a': 1}"``) — the two will never agree
        on a canonical text form for a composite value, and reconciling
        JSON-serialization vs Python-repr is not attempted here (a
        composite ``pack_id`` is a data error, not a value either side
        needs to correctly rank-and-file). See ``_SqlGraphStoreBase._expand``
        for why the Python-side filter staying in place still matters for
        this one gap.

        Used by ``_SqlGraphStoreBase._pack_where`` for both the node- and
        edge-side pack_id checks so the pushed-down SQL predicate and
        ``_node_passes``/``_edge_passes`` can never disagree on what counts
        as "no pack_id" (see issue #62 comment thread, cluster 5 —
        empty-vs-absent divergence).
        """
        raw = self.json_get(col, key)
        if self.name == "sqlite":
            typ = f"json_type({col}, '$.{key}')"
            return (
                f"(CASE {typ}"
                f" WHEN 'null' THEN NULL"
                f" WHEN 'false' THEN NULL"
                f" WHEN 'true' THEN 'True'"
                f" WHEN 'text' THEN NULLIF({raw}, '')"
                f" WHEN 'integer' THEN NULLIF(CAST({raw} AS TEXT), '0')"
                f" WHEN 'real' THEN NULLIF(CAST({raw} AS TEXT), '0.0')"
                f" ELSE CAST({raw} AS TEXT)"  # missing key, object, array
                f" END)"
            )
        node = f"{col}->'{key}'"
        typ = f"jsonb_typeof({node})"
        return (
            f"(CASE {typ}"
            f" WHEN 'null' THEN NULL"
            f" WHEN 'boolean' THEN (CASE WHEN {raw} = 'true' THEN 'True' ELSE NULL END)"
            f" WHEN 'string' THEN NULLIF({raw}, '')"
            # Text comparison alone would miss "0.0"/"0.00"/"-0" — jsonb's
            # ->> preserves the number's original literal spelling, unlike
            # SQLite's CAST (see docstring), so this compares numerically.
            f" WHEN 'number' THEN (CASE WHEN ({node})::numeric = 0 THEN NULL ELSE {raw} END)"
            f" ELSE {raw}"  # missing key, object, array
            f" END)"
        )

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

    def in_string_array(
        self, expr: str, placeholder: str
    ) -> tuple[str, Callable[[list[str]], Any]]:
        """Array-bind membership test: ``expr`` IN a caller-supplied list,
        using exactly ONE bind parameter no matter how many values are in
        the list -- NOT ``_SqlGraphStoreBase._in_placeholders``'s
        one-bind-per-value expansion.

        WHY THIS EXISTS (issue #147 §3.4(c)): the read-scoping this method
        was added for pushes ``readable_pack_ids(principal)`` into a WHERE
        clause -- a set that legitimately spans every non-private pack in
        the deployment, unbounded by anything the caller authored (unlike
        the small, hand-typed ``pack_ids`` filters ``_pack_where`` and
        ``_export_nodes_where`` were originally sized for). One bind per
        value blows past SQLite's ``SQLITE_MAX_VARIABLE_NUMBER`` (as low as
        999 on some builds) with "too many SQL variables" once the scope
        is large. Chunking the ``IN`` list instead was considered and
        rejected at the design stage: an edge predicate that requires BOTH
        endpoints' pack_id to match (see ``_pack_where``'s AND-of-two-
        clauses shape) drops any edge whose two endpoints land in
        different chunks, permanently, independent of ``limit`` -- see
        issue #147 design §3.4(c). One array bind has none of that
        failure mode: exactly one placeholder, independent of scope size.

        ``placeholder`` IS AN ARGUMENT, not hardcoded to ``":packs"``,
        because bind style varies by CALL SITE, not by dialect: some call
        sites bind NAMED (``:name``) throughout, while one that executes
        raw ``sqlite3`` uses POSITIONAL (``?``) placeholders with a
        ``params: list`` -- and mixing qmark and named placeholders in one
        SQLite statement raises ``sqlite3.ProgrammingError``. A hardcoded
        ``:packs`` would compile fine here in isolation and only break at
        the SQL-execution boundary of that one caller, far from this
        function -- so the placeholder token is threaded through instead.

        Returns ``(sql_fragment, value_transform)``. ``value_transform``
        is what the caller must apply to its Python ``list[str]`` BEFORE
        binding it as ``placeholder``'s value:
          - SQLite: ``json_each(...)`` is a table-valued function that
            reads a JSON array TEXT value, not a Python list object, so
            the transform is ``json.dumps``.
          - PostgreSQL: ``ANY(CAST(... AS text[]))`` binds a Python list
            directly -- the driver (psycopg2, via SQLAlchemy) adapts it to
            a PG array literal -- so the transform is the identity
            (``list``, which also defensively copies rather than aliasing
            the caller's list). This mirrors the existing
            ``pg_graph_store.py::_batch_frontier_edges`` /
            ``_batch_node_props_multi`` precedent of binding a Python list
            straight into ``CAST(:ids AS text[])`` for ``unnest(...)`` --
            reusing that established pattern rather than introducing a
            second PG array-binding convention.
        """
        if self.name == "sqlite":
            return f"{expr} IN (SELECT value FROM json_each({placeholder}))", json.dumps
        return f"{expr} = ANY(CAST({placeholder} AS text[]))", list

    # ------------------------------------------------------------------
    # INSERT / UPSERT
    # ------------------------------------------------------------------

    def _value_exprs(self, columns: Sequence[str], json_columns: Sequence[str]) -> list[str]:
        """Both dialects render NAMED (``:col``) placeholders here — sqlite3
        accepts paramstyle "named" natively (see module docstring), so using
        it uniformly lets a shared call site pass ONE params dict to either
        backend's execute call instead of shaping the params twice. Only
        PG's JSON columns get the ``CAST(... AS jsonb)`` wrapper; SQLite has
        no such cast (JSON is stored as plain TEXT)."""
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
        """One schema spec -> this dialect's DDL statement list, emitted
        CREATE TABLE first and then that table's indexes, table by table.

        ``schema_name`` is only meaningful for PG (qualifies each statement
        with ``"schema".table``); SQLite has no schema concept here and
        ignores it.
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
                    if col.not_null:
                        line += " NOT NULL"
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
