"""
_SqlDocStoreBase — shared implementation of the 13-method doc-store surface
(upsert_node_doc, get_node_doc, list_nodes, bm25_fingerprint, delete_node_doc,
upsert_source, keyword_search, get_source, list_sources, log_event,
get_audit_log, collection_stats, ping), parameterised by a ``SqlDialect``
(SQLITE or POSTGRES from ``_sql_dialect.py``).

STATUS: wired. LocalSQLDocStore and PgDocStore both subclass this base --
the Stage 6a "F1 (SQLite) / F2 (PG) adopter follow-up" this paragraph used
to describe as pending has since landed, and the claim that it is "NOT yet
wired" was stale (noticed while adding the pack-scoped read methods here in
#147, since one of them is an authorization path and a reader needs to know
it actually runs). factory.py, the two stores' public class names, and their
module paths are unchanged by this file's existence.

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
from collections.abc import Callable
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
        # list_nodes()'s ORDER BY updated_at DESC, space, node_id (#63 tie-
        # breaker, codex P2) needs its own composite index in this exact
        # column order + direction, or SQLite/PG fall back to a temp B-tree
        # sort instead of walking the index — measured 3x slower at live
        # scale (253k rows: 435ms indexed sort vs 1303ms temp-B-tree, see
        # PR #100). idx_doc_nodes_updated (above) is kept: bm25_fingerprint's
        # bare MAX(updated_at) and any other single-column updated_at lookup
        # still use it fine.
        IndexSpec("idx_doc_nodes_updated_tiebreak", "doc_nodes", "updated_at DESC, space, node_id"),
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


def _json_str_in(
    dialect: SqlDialect, col: str, key: str, placeholder: str
) -> tuple[str, Callable[[list[str]], Any]]:
    """Array-bind sibling of ``opencrab/pack/load.py``'s ``_json_str_eq`` --
    same STRING-TYPE-ONLY strictness (the JSON/JSONB value must actually be
    typed ``text``/``string``, not merely stringify-equal -- see that
    function's docstring for why: ``pack_id``/``source`` are measured
    all-string in live data, so narrowing to string-typed matches costs
    nothing today and keeps a future non-string ``pack_id`` correctly
    non-matching instead of comparing-as-text), but tests membership in a
    caller-supplied LIST instead of equality to one bound scalar.

    Built from ``SqlDialect.in_string_array``'s single-bind array machinery
    (see that docstring for why one bind beats one-per-value -- SQLite's
    ``SQLITE_MAX_VARIABLE_NUMBER`` is as low as 999, and
    ``list_sources_scoped`` inherits the same "well past that" requirement
    ``list_nodes_scoped`` was built for) applied to the same typed JSON
    extraction ``_json_str_eq`` uses.
    """
    raw = dialect.json_get(col, key)
    frag, transform = dialect.in_string_array(raw, placeholder)
    type_check = (
        f"json_type({col}, '$.{key}') = 'text'"
        if dialect.name == "sqlite"
        else f"jsonb_typeof({col}->'{key}') = 'string'"
    )
    return f"({type_check} AND {frag})", transform


def _doc_owner_pred_scoped(
    dialect: SqlDialect, placeholder: str
) -> tuple[str, Callable[[list[str]], Any]]:
    """Array-bind sibling of ``opencrab/pack/load.py``'s ``_doc_owner_pred``
    -- the SAME canon, REPLICATED rather than imported (issue #201 §4-B).
    ``_doc_owner_pred`` is single-scalar (``metadata.pack_id == :pack``,
    built for ``delete_pack``/``build_count_sql``'s one-pack-at-a-time
    callers); this file's ``*_scoped`` reads need list membership, matching
    ``list_nodes_scoped``'s existing precedent for this method family.
    Importing ``opencrab.pack.load`` into ``opencrab.stores`` here would
    also reach a pack-layer module back across the store-layer boundary
    this base module has otherwise stayed under. Since a direct import
    isn't taken, this docstring is the tripwire: if ``_doc_owner_pred``'s
    OR/fallback shape ever changes, this predicate must change with it, or
    a fork's preflight read (``pack_fork``, design §5-1 step 4) and the
    incremental loader's actual copy (``_doc_owner_pred``) will silently
    disagree about which sources belong to a pack.

    SAME shape as ``_doc_owner_pred``, parenthesised the same way so a
    caller's later ``AND {pred}`` cannot silently change which branch it
    binds to:

        (pack_id string-matches the list)
        OR (pack_id is absent/falsy AND source string-matches the list)

    ``pack_id`` is the owning key; ``source`` is only consulted when
    ``pack_id`` is absent -- a mixed-tag row (``pack_id="B", source="A"``)
    must NOT fall into A's scope (the exact bug ``_doc_owner_pred``'s
    docstring documents fixing; an unconditional OR would re-introduce it
    here independently). "Absent" reuses ``SqlDialect.json_truthy_text`` --
    the same falsy definition (missing / JSON null / ``""``/``0``/``false``)
    ``_doc_owner_pred`` itself uses, so a doc row and its graph twin can
    never disagree about whether it has a pack_id.

    Both string-membership fragments bind the SAME ``placeholder`` (the
    caller passes one list of pack_ids once) -- named parameters may repeat
    within one statement on both dialects (see ``build_count_sql``'s
    ``:pack`` reuse for the established precedent), so the caller supplies
    exactly one bound value under this name.
    """
    pack_frag, transform = _json_str_in(dialect, "metadata", "pack_id", placeholder)
    source_frag, _ = _json_str_in(dialect, "metadata", "source", placeholder)
    pack_absent = f"{dialect.json_truthy_text('metadata', 'pack_id')} IS NULL"
    return f"(({pack_frag}) OR ({pack_absent} AND ({source_frag})))", transform


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
        """``limit <= 0`` (issue #120 follow-up): returns ``[]`` without
        querying, same contract as ``_sql_graph_base.py``'s ``export_nodes``
        -- SQLite maps a bound ``LIMIT -1`` to "no limit", so without this
        guard a negative ``limit`` here would return the entire table."""
        self._require_available()
        if limit <= 0:
            return []
        table = self._table("doc_nodes")
        # ORDER BY updated_at DESC: 정렬 없이 LIMIT 만 걸면 어느 행이 뽑힐지 SQL 표준상
        # 보장되지 않는다(#63). 최신순으로 고정해 상한을 넘는 코퍼스에서도 최소한
        # 최근 변경분은 검색 가능하고, 선택 결과가 결정적이도록 한다.
        # tie-breaker (space, node_id): updated_at 이 동률인 행이 상한 경계에 여럿
        # 있으면(배치 적재·마이그레이션에서 흔함) updated_at 만으로는 그 안에서 순서가
        # 여전히 미정이라 매번 다른 부분집합이 뽑힐 수 있다. (space, node_id) 는 PK라
        # 전순서를 보장한다(codex P2).
        if space:
            sql = (
                f"SELECT space, node_id, node_type, properties, updated_at"
                f" FROM {table} WHERE space=:space"
                f" ORDER BY updated_at DESC, space, node_id LIMIT :lim"
            )
            params = {"space": space, "lim": limit}
        else:
            sql = (
                f"SELECT space, node_id, node_type, properties, updated_at"
                f" FROM {table} ORDER BY updated_at DESC, space, node_id LIMIT :lim"
            )
            params = {"lim": limit}
        rows = self._fetch_all(sql, params)
        return [self._row_to_node(r) for r in rows]

    def list_nodes_scoped(
        self, pack_ids: list[str], space: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Authorization-scoped counterpart to ``list_nodes`` (issue #147
        §3.5) -- built for ``ontology_list_nodes``'s pack-UNSPECIFIED branch
        (see §3.5's 7-caller table for why ``list_nodes`` itself stays
        untouched: its other 6 callers are BM25/index-build paths, not
        user-response paths). Issue #55 switched that branch to the graph
        store instead (so the two node-lookup MCP tools stop disagreeing on
        which store answers), which leaves this method with no in-repo
        caller -- kept as part of this store's own scoped-read API, not
        removed, since #55's scope is the MCP tool layer, not this store's
        surface.

        Same ``ORDER BY updated_at DESC, space, node_id`` tie-breaker as
        ``list_nodes`` (issue #63 -- unordered ``LIMIT`` has no
        SQL-standard-guaranteed row selection), PLUS
        ``json_truthy_text(properties,'pack_id') IN <array bind>``. A
        SINGLE clause is enough here (unlike the graph store's
        ``_scoped_node_where``, which ANDs a second ``IS NOT NULL`` clause
        onto a bare ``json_get`` membership test for index reasons) --
        ``doc_nodes`` has no pack_id index to preserve (see
        ``DOC_STORE_SCHEMA``'s index list), and ``json_truthy_text``
        already returns SQL NULL for a missing/falsy pack_id, which never
        satisfies ``IN``/``= ANY(...)`` membership on either dialect -- so
        an unpackaged row is excluded by the SAME clause that does the
        scope check, with nothing extra to add.

        Empty ``pack_ids`` -> ``[]`` WITHOUT querying (nothing is in scope,
        so there is nothing to fetch) -- unlike ``list_nodes``, which has
        no scope concept and returns everything up to ``limit`` for
        ``space=None``. ``limit <= 0`` -> ``[]``, same guard ``list_nodes``
        uses (issue #120 follow-up)."""
        self._require_available()
        if not pack_ids or limit <= 0:
            return []
        table = self._table("doc_nodes")
        pid_expr = self._dialect.json_truthy_text("properties", "pack_id")
        pack_frag, transform = self._dialect.in_string_array(pid_expr, ":sc_packs")
        where_parts = [pack_frag]
        params: dict[str, Any] = {"sc_packs": transform(sorted(set(pack_ids)))}
        if space:
            where_parts.append("space=:space")
            params["space"] = space
        params["lim"] = limit
        sql = (
            f"SELECT space, node_id, node_type, properties, updated_at"
            f" FROM {table} WHERE {' AND '.join(where_parts)}"
            f" ORDER BY updated_at DESC, space, node_id LIMIT :lim"
        )
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
        """``limit <= 0``: returns ``[]`` without querying -- same contract
        as ``list_nodes`` above."""
        self._require_available()
        if limit <= 0:
            return []
        sql = (
            f"SELECT source_id, text, metadata, ingested_at"
            f" FROM {self._table('doc_sources')} LIMIT :lim"
        )
        rows = self._fetch_all(sql, {"lim": limit})
        return [self._row_to_source(r) for r in rows]

    def list_sources_scoped(self, pack_ids: list[str], limit: int = 100) -> list[dict[str, Any]]:
        """Authorization-scoped counterpart to ``list_sources`` (issue #201
        §4-B) -- gives ``pack_fork``'s preflight (design §5-1 step 4) a way
        to read a pack's ``doc_sources`` rows; nothing in this file could do
        that before (only ``list_nodes_scoped`` existed, for ``doc_nodes``).

        Ownership predicate: ``_doc_owner_pred_scoped`` above, the list-bind
        replica of ``opencrab/pack/load.py``'s ``_doc_owner_pred`` canon --
        see that helper's docstring for why ``pack_id`` beats ``source``
        only when ``pack_id`` is absent, not an unconditional OR.

        ``limit <= 0`` or empty ``pack_ids`` -> ``[]`` WITHOUT querying,
        same contract as ``list_nodes_scoped`` above.

        FAIL-CLOSED ON UNAVAILABLE (design §5-1 step 3, issue #201): this
        method RAISES (via ``_require_available()``) rather than returning
        ``[]`` when the store is down. ``pack_fork`` treats an empty result
        as "this pack genuinely has zero sources" and, on that basis, skips
        every copied chunk-vector record whose id isn't in that source set
        as an orphan (design §5-1 step 6b, ``skipped.vector_orphans``). If
        an outage produced the same empty list as a truly-empty pack, fork
        would skip every chunk vector as an orphan and still report ``ok``
        on a copy that silently lost all its chunks -- an outage must be
        distinguishable from "nothing here", or it becomes silent data loss
        dressed up as success.
        """
        self._require_available()
        if not pack_ids or limit <= 0:
            return []
        table = self._table("doc_sources")
        pred, transform = _doc_owner_pred_scoped(self._dialect, ":sc_packs")
        params: dict[str, Any] = {
            "sc_packs": transform(sorted(set(pack_ids))),
            "lim": limit,
        }
        # ORDER BY ingested_at DESC, source_id: same determinism reasoning
        # as list_nodes_scoped's tie-breaker (issue #63) -- an unordered
        # LIMIT has no SQL-standard-guaranteed row selection, and
        # pack_fork's CAP+1 truncation-detection read (design §5-1 step 4)
        # needs a stable row selection for "did we get CAP+1 back" to mean
        # the same thing across repeated calls.
        sql = (
            f"SELECT source_id, text, metadata, ingested_at"
            f" FROM {table} WHERE {pred}"
            f" ORDER BY ingested_at DESC, source_id LIMIT :lim"
        )
        rows = self._fetch_all(sql, params)
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
        """``limit <= 0``: returns ``[]`` without querying -- same contract
        as ``list_nodes`` above."""
        self._require_available()
        if limit <= 0:
            return []
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
