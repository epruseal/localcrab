"""
Local graph store — SQLite-backed graph for local-only mode.

Adopter of ``_SqlGraphStoreBase`` (Stage 6b F3): the 17 shared graph-store
methods (upsert_node, get_node, lookup_node_type, delete_node, upsert_edge,
run_cypher, find_neighbors, find_path, count_nodes, list_packs,
find_by_relations, get_node_by_id, export_nodes, export_edges,
upsert_nodes_batch, upsert_edges_batch, ensure_constraints) now live in
``_sql_graph_base.py``, parameterised by ``SQLITE``. This module supplies the
SQLite-specific plumbing the base's adoption contract requires: connection
hooks (via ``_SqliteConnMixin``) and lifecycle (``__init__``/``_init_db``/
``available``/``ping``).

THREAD SAFETY: each thread gets its own sqlite3 connection (threading.local) —
    sharing one connection across threads corrupts even reads. WAL mode lets
    those per-thread connections read concurrently while a threading.Lock
    serialises writers so only one connection writes the file at a time
    (avoids SQLITE_BUSY). Reads take no lock.

PERF NOTE (find_neighbors hub fan-out): this adopter does NOT override
    ``_prefetch_frontier`` — the base's default (one per-node query per BFS
    level, capped at the level-wide ``limit``) was benchmarked against a
    ~10k-out-edge hub node vs. the pre-adoption "live remaining" SQL LIMIT and
    found to not regress materially (measured ~1.2x, well under threshold) —
    the historical 32x hub-fanout fix
    (see git history for this file) is preserved by the *inner* `_expand`
    loop's `[:remaining]` slice + early `break`, both still present in
    ``_sql_graph_base.py``'s ``_expand``, so results are unaffected either way.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencrab.common.graph_identity import GraphSchemaMigrationRequired
from opencrab.stores._json import parse_props  # noqa: F401 — re-exported for tests/callers
from opencrab.stores._sql_dialect import SQLITE
from opencrab.stores._sql_graph_base import GRAPH_STORE_SCHEMA, GraphTx, _SqlGraphStoreBase
from opencrab.stores._sqlite_base import _SqliteConnMixin

logger = logging.getLogger(__name__)


class LocalGraphStore(_SqliteConnMixin, _SqlGraphStoreBase):
    """SQLite-backed graph store with the same interface as Neo4jStore."""

    _dialect = SQLITE

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self._available = False
        self._schema_state = "unconfigured"
        self._init_conn_state(db_path)
        self._init_db()

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        """search_nodes()'s keyword predicate (_sql_graph_base.py) matches
        via SQL ``LOWER(...) LIKE``, but SQLite's builtin ``LOWER()`` is
        ASCII-only -- a stored "FÜR" stays "FÜR" (issue #86 verifier
        finding), so it never matches a "für" keyword even though the
        keyword itself IS lowered (in Python, which IS Unicode-aware) before
        binding. Overriding the SQL ``lower`` function with Python's
        ``str.lower`` makes both sides of the comparison use the same
        Unicode-aware lowering -- matching what the OLD Python-only
        `keyword_search` did on both sides, and matching PG's already
        locale-aware ``LOWER()`` / Kuzu's Python-side ``.lower()`` (search_nodes
        never leaves Python there), so all three backends agree again.

        ``str(s).lower()``, not bare ``s.lower()`` (issue #86 2nd verifier
        finding): a non-string property value (e.g. ``{"name": 12345}``) hit
        this UDF and raised, which sqlite3 propagates as an
        ``OperationalError`` out of the whole query -- crashing
        ``search_nodes()`` for every node, not just the offending one. The
        builtin ``LOWER()`` it replaces silently coerces
        (``LOWER(123) = '123'``), and so do the OLD Python-only
        `keyword_search` (``str(val).lower()``) and Kuzu's ``search_nodes``
        (``str(props[f]).lower()``) -- ``str(s).lower()`` here matches all
        three instead of introducing a 4th, stricter behaviour."""
        conn.create_function("lower", 1, lambda s: None if s is None else str(s).lower())

    def _init_db(self) -> None:
        try:
            conn = self._conn  # 이 스레드 커넥션 생성 + WAL pragma 적용
            state = self._classify_schema(conn)
            if state == "fresh":
                with self._tx(immediate=True) as tx_conn:
                    for ddl in SQLITE.render_ddl(GRAPH_STORE_SCHEMA):
                        tx_conn.execute(ddl)
                state = self._classify_schema(conn)
            self._schema_state = "target" if state == "target" else ("legacy_migration_required" if state == "legacy" else "partial_or_unknown")
            # A connected legacy/partial database remains readable. Mutation
            # methods use the separate schema gate in the shared base.
            self._available = state in {"target", "legacy", "partial"}
            logger.info("LocalGraphStore initialised at %s", self._db_path)
        except Exception as exc:
            self._schema_state = "partial_or_unknown"
            logger.warning("LocalGraphStore init failed: %s", exc)

    def _classify_schema(self, conn: sqlite3.Connection) -> str:
        all_objects = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table','index','trigger','view')"
        ).fetchall()
        table_names = {r[1] for r in all_objects if r[0] == "table"}
        graph_tables = {"graph_nodes", "graph_edges"}
        # Staging, old-suffix, and half-renamed graph objects are recovery
        # residue, never a fresh database.  This also catches a graph-owned
        # table that is present without either canonical table.
        graph_prefixes = ("graph_nodes__", "graph_edges__", "graph_nodes_", "graph_edges_")
        if any(name.startswith(graph_prefixes) for name in table_names):
            return "partial"
        if not (table_names & graph_tables):
            graph_sql = " ".join((r[3] or "").lower() for r in all_objects if r[0] in ("trigger", "view"))
            if "graph_nodes" in graph_sql or "graph_edges" in graph_sql:
                return "partial"
            return "fresh"
        if table_names & graph_tables != graph_tables:
            return "partial"

        def table_info(name: str) -> tuple[tuple[tuple[Any, ...], ...], str]:
            rows = conn.execute(f"PRAGMA table_xinfo({name})").fetchall()
            # name, declared type, nullability, PK ordinal, default, hidden
            meta = tuple((r[1], (r[2] or "").upper(), int(r[3]), int(r[5]), r[4], int(r[6])) for r in rows)
            sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=:name", {"name": name}
            ).fetchone()
            return meta, (sql_row[0] or "") if sql_row else ""

        node_meta, node_sql = table_info("graph_nodes")
        edge_meta, edge_sql = table_info("graph_edges")

        def expected_meta(table: Any) -> tuple[tuple[Any, ...], ...]:
            out = []
            pk_ord = {name: ordinal for ordinal, name in enumerate(table.primary_key, 1)}
            for ordinal, column in enumerate(table.columns, 1):
                default = "'{}'" if column.default == "{}" else None
                out.append((column.name, "TEXT", int(column.not_null), pk_ord.get(column.name, 0), default, 0))
            return tuple(out)

        target_node = expected_meta(GRAPH_STORE_SCHEMA.tables[0])
        target_edge = expected_meta(GRAPH_STORE_SCHEMA.tables[1])
        legacy_node = (
            ("node_type", "TEXT", 1, 1, None, 0),
            ("node_id", "TEXT", 1, 2, None, 0),
            ("space_id", "TEXT", 0, 0, None, 0),
            ("properties", "TEXT", 1, 0, "'{}'", 0),
        )
        legacy_edge = (
            ("from_type", "TEXT", 1, 1, None, 0),
            ("from_id", "TEXT", 1, 2, None, 0),
            ("relation", "TEXT", 1, 3, None, 0),
            ("to_type", "TEXT", 1, 4, None, 0),
            ("to_id", "TEXT", 1, 5, None, 0),
            ("properties", "TEXT", 1, 0, "'{}'", 0),
        )

        # SQLite can report a PK column's nullability differently for some
        # historical table declarations.  The target renderer explicitly
        # declares every key column NOT NULL, so the metadata check remains
        # strict and rejects mixed or hand-edited schemas.
        def has_forbidden_table_objects(sql: str) -> bool:
            upper = sql.upper()
            return any(token in upper for token in ("CHECK", "REFERENCES", " UNIQUE", "WITHOUT ROWID", "STRICT"))

        if has_forbidden_table_objects(node_sql) or has_forbidden_table_objects(edge_sql):
            return "partial"
        if conn.execute("PRAGMA foreign_key_list(graph_nodes)").fetchall() or conn.execute("PRAGMA foreign_key_list(graph_edges)").fetchall():
            return "partial"

        def indexes_match() -> bool:
            expected = {"idx_nodes_pack", "idx_nodes_space", "idx_edges_from", "idx_edges_to"}
            actual: list[tuple[str, str, int, int, int, tuple[str | None, ...], str]] = []
            for table in graph_tables:
                rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
                for row in rows:
                    name, unique, origin, partial = row[1], int(row[2]), row[3], int(row[4])
                    if str(name).startswith("sqlite_autoindex"):
                        if origin != "pk" or unique != 1:
                            return False
                        continue
                    xinfo = conn.execute(f"PRAGMA index_xinfo({name})").fetchall()
                    columns = tuple(x[2] for x in sorted((x for x in xinfo if int(x[0]) >= 0 and x[2] is not None), key=lambda x: x[0]))
                    sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name", {"name": name}).fetchone()
                    actual.append((name, table, unique, partial, 0 if origin == "c" else 1, columns, (sql_row[0] or "") if sql_row else ""))
            if len(actual) != 4 or any(a[2] != 0 or a[3] != 0 or a[4] != 0 for a in actual):
                return False
            matched: set[str] = set()
            for _name, table, _unique, _partial, _origin, columns, sql in actual:
                normalized = "".join(sql.lower().split())
                if table == "graph_nodes" and columns == ("space_id",):
                    matched.add("idx_nodes_space")
                elif table == "graph_nodes" and "json_extract(properties,'$.pack_id')" in normalized:
                    matched.add("idx_nodes_pack")
                elif table == "graph_edges" and columns == ("from_id",):
                    matched.add("idx_edges_from")
                elif table == "graph_edges" and columns == ("to_id",):
                    matched.add("idx_edges_to")
                else:
                    return False
            return matched == expected

        def dependency_tokens(sql: str) -> str:
            """Remove comments and string literals before view dependency scan."""
            out: list[str] = []
            i = 0
            while i < len(sql):
                if sql.startswith("--", i):
                    end = sql.find("\n", i + 2)
                    i = len(sql) if end < 0 else end
                    continue
                if sql.startswith("/*", i):
                    end = sql.find("*/", i + 2)
                    if end < 0:
                        return ""
                    i = end + 2
                    continue
                if sql[i] == "'":
                    i += 1
                    while i < len(sql):
                        if sql[i] == "'":
                            if i + 1 < len(sql) and sql[i + 1] == "'":
                                i += 2
                                continue
                            i += 1
                            break
                        i += 1
                    continue
                out.append(sql[i])
                i += 1
            return "".join(out)

        # Any trigger/view attached to or referring to either graph table is
        # graph-owned residue.  Unrelated objects in the same database are
        # intentionally ignored.  String literals are not dependencies, so a
        # documentation view containing the text "graph_nodes" stays unrelated.
        for kind, _name, tbl_name, sql in all_objects:
            if kind not in ("trigger", "view"):
                continue
            text = dependency_tokens(sql or "").lower()
            if tbl_name in graph_tables or any(re.search(rf"\b{table}\b", text) for table in graph_tables):
                return "partial"

        target = node_meta == target_node and edge_meta == target_edge and indexes_match()
        if target:
            return "target"
        legacy = node_meta == legacy_node and edge_meta == legacy_edge
        # Legacy databases use the same four secondary indexes.  The primary
        # key is intentionally the only semantic distinction here.
        if legacy and indexes_match():
            return "legacy"
        return "partial"

    @property
    def schema_state(self) -> str:
        return self._schema_state

    def _require_schema_available(self) -> None:
        if self._schema_state == "legacy_migration_required":
            raise GraphSchemaMigrationRequired("graph schema requires issue80 migration before writes")
        if self._schema_state == "partial_or_unknown":
            raise GraphSchemaMigrationRequired("graph schema is unavailable: migration required")
        if not self._available:
            raise RuntimeError(f"{type(self).__name__} is not available.")

    def _require_available(self) -> None:
        if not self._available:
            raise RuntimeError(f"{type(self).__name__} is not available.")

    def _run_graph_tx(self, callback: Callable[[GraphTx], Any], *, immediate: bool = False, snapshot_path: Path | None = None) -> Any:
        """Run one graph callback on the writer connection.

        A graph snapshot is deliberately an executor concern.  It is made
        with SQLite's backup API while ``BEGIN IMMEDIATE`` and the mixin's
        writer lock are held, then installed with ``os.replace`` before the
        callback can mutate the live transaction.  The callback only gets
        ``GraphTx`` and therefore cannot accidentally write the destination
        connection or issue transaction controls itself.
        """
        if snapshot_path is not None and not immediate:
            raise ValueError("snapshot_path requires immediate=True")
        if self._graph_tx_is_active():
            raise RuntimeError("nested graph transaction is not allowed")

        destination = None
        temporary = None
        destination_path: Path | None = None
        if snapshot_path is not None:
            destination_path = Path(snapshot_path)
            source_path = Path(self._db_path).absolute()
            if not destination_path.is_absolute() or destination_path.absolute() == source_path:
                raise ValueError("snapshot_path must be an absolute path different from the source")
            parent = destination_path.parent
            if not parent.is_dir():
                raise RuntimeError("graph snapshot creation failed")
            try:
                fd, temporary = tempfile.mkstemp(
                    prefix=f".{destination_path.name}.issue80-",
                    suffix=".tmp",
                    dir=str(parent),
                )
                os.close(fd)
                os.unlink(temporary)
                with self._tx(immediate=True) as source:
                    # The destination is intentionally not exposed to the
                    # callback.  Python's SQLite builds commonly block when
                    # ``Connection.backup`` is called on the connection that
                    # owns an active IMMEDIATE transaction.  Use the
                    # documented read-source fallback while the writer lock
                    # remains held; it calls the same backup API and copies
                    # the committed WAL state consistently.
                    destination = sqlite3.connect(temporary, timeout=0)
                    try:
                        read_source = sqlite3.connect(self._db_path, timeout=0)
                        try:
                            read_source.backup(destination)
                        except Exception as backup_exc:
                            raise RuntimeError("graph snapshot creation failed") from backup_exc
                        finally:
                            read_source.close()
                    except RuntimeError:
                        raise
                    except BaseException as exc:
                        raise RuntimeError("graph snapshot creation failed") from exc
                    try:
                        destination.close()
                    except Exception as close_exc:
                        raise RuntimeError("graph snapshot creation failed") from close_exc
                    destination = None
                    try:
                        os.replace(temporary, destination_path)
                    except Exception as exc:
                        raise RuntimeError("graph snapshot creation failed") from exc
                    temporary = None
                    self._set_graph_tx_active(True)
                    try:
                        return callback(GraphTx(source, self._dialect))
                    finally:
                        self._set_graph_tx_active(False)
            finally:
                if destination is not None:
                    try:
                        destination.close()
                    except Exception:
                        pass
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass

        self._set_graph_tx_active(True)
        try:
            with self._tx(immediate=immediate) as source:
                return callback(GraphTx(source, self._dialect))
        finally:
            self._set_graph_tx_active(False)

    @property
    def available(self) -> bool:
        return self._available

    def ping(self) -> bool:
        try:
            assert self._conn
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # _SqlGraphStoreBase hooks
    # ------------------------------------------------------------------

    def _table(self, name: str) -> str:
        return name

    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[tuple]:
        return self._conn.execute(sql, params).fetchall()

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> tuple | None:
        return self._conn.execute(sql, params).fetchone()

    # ``_require_available`` is inherited from ``_SqliteConnMixin``.
