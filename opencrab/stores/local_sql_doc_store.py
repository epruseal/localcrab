"""
Local SQL document store — SQLite-backed doc store for local-only mode.

Replaces LocalDocStore (JSON-file) and provides the same interface as
MongoStore so consumers are agnostic of the backend.

WHY SQLite INSTEAD OF JSON:
    LocalDocStore._load() deserializes the entire JSON file on every read and
    _save() re-serializes the entire dataset on every write — O(N) per
    operation.  With list_nodes(limit=50000) called on every BM25 cache
    rebuild (every query), a 10× data growth would make each rebuild 10×
    slower with no way to offset it.

    SQLite uses B-tree pages; a PK lookup is O(log N) and a range scan with
    LIMIT skips unneeded rows entirely.  WAL mode lets readers and writers
    proceed concurrently, which is critical for MCP servers.

SCHEMA DESIGN:
    Three tables mirror the three logical collections in LocalDocStore /
    MongoStore:
        doc_nodes   — upserted node docs (space × node_id PK)
        doc_sources — ingested source records (source_id PK)
        audit_log   — append-only event log (uuid4 PK, indexed by timestamp)

    properties / metadata / details are stored as JSON TEXT.  Structured
    columns are avoided because the dict schema is open and varies by caller.
    json_extract() would add SQLite >= 3.38 dependency; caller-side json.loads
    keeps the version floor at 3.9.0 (same as local_graph_store.py).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = [
    # doc_nodes: primary key on (space, node_id) → O(log N) PK lookup.
    # updated_at index supports time-range queries added in the future and
    # lets the DB engine sort without a full-table pass.
    """
    CREATE TABLE IF NOT EXISTS doc_nodes (
        space       TEXT NOT NULL,
        node_id     TEXT NOT NULL,
        node_type   TEXT NOT NULL DEFAULT '',
        properties  TEXT NOT NULL DEFAULT '{}',
        updated_at  TEXT NOT NULL,
        PRIMARY KEY (space, node_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_doc_nodes_updated ON doc_nodes(updated_at)",
    # doc_sources: simple PK on source_id.
    """
    CREATE TABLE IF NOT EXISTS doc_sources (
        source_id   TEXT PRIMARY KEY,
        text        TEXT NOT NULL DEFAULT '',
        metadata    TEXT NOT NULL DEFAULT '{}',
        ingested_at TEXT NOT NULL
    )
    """,
    # audit_log: uuid4 PK prevents duplicates even under concurrent writers.
    # timestamp DESC index avoids a full-table sort on every get_audit_log call.
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        event_id    TEXT PRIMARY KEY,
        event_type  TEXT NOT NULL,
        subject_id  TEXT,
        details     TEXT NOT NULL DEFAULT '{}',
        timestamp   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp DESC)",
]


class LocalSQLDocStore:
    """SQLite-backed document store with the same interface as MongoStore /
    LocalDocStore.

    All writes use INSERT OR REPLACE (UPSERT) so callers can call upsert_*
    methods unconditionally without managing existence checks.

    Thread-safety: sqlite3.connect(check_same_thread=False) + WAL mode allows
    multiple threads to share a single connection safely — WAL guarantees
    serialised writes and non-blocking reads.
    """

    def __init__(self, db_path: str) -> None:
        """
        Replaces: LocalDocStore.__init__(data_dir) / MongoStore.__init__(uri, db_name)
        WHY: receive a file path rather than a directory so the caller controls
             exactly where the DB lives (simpler than data_dir + filename logic).
        THREAD SAFETY: a threading.Lock serialises all write operations so that
             multiple threads sharing a single connection never interleave
             commits.  SQLite's check_same_thread=False allows cross-thread use
             of the connection object; the Lock ensures only one thread is
             executing a write at a time.
        """
        self._db_path = db_path
        self._available = False
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """
        Replaces: LocalDocStore's implicit dir creation + LocalGraphStore._init_db()
        WHY: WAL + synchronous=NORMAL is the same pattern as local_graph_store.py.
             Creates all three tables in a single transaction for atomicity.

        PRAGMA details (copied rationale from local_graph_store.py):
            WAL mode: readers never block writers and writers never block readers.
            synchronous=NORMAL: fsync only at WAL checkpoint, not every commit.
                Acceptable risk for a single-machine local store.
        """
        try:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            cur = self._conn.cursor()
            for ddl in _DDL:
                cur.execute(ddl)
            self._conn.commit()
            self._available = True
            logger.info("LocalSQLDocStore initialised at %s", self._db_path)
        except Exception as exc:
            logger.warning("LocalSQLDocStore init failed: %s", exc)
            self._available = False

    # ------------------------------------------------------------------
    # Availability / lifecycle
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Replaces: LocalDocStore.available / MongoStore.available"""
        return self._available

    def ping(self) -> bool:
        """
        Replaces: LocalDocStore.ping() (os.path.isdir) / MongoStore.ping()
        WHY: a real DB round-trip is more accurate than a filesystem stat —
             detects corrupted connections that the path check would miss.
        """
        try:
            assert self._conn
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        """Replaces: MongoStore.close() (LocalDocStore had no close())"""
        if self._conn:
            try:
                self._conn.close()
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
        """
        Replaces: LocalDocStore.upsert_node_doc / MongoStore.upsert_node_doc
        WHY SQLite: JSON backend re-serializes the entire nodes dict on every
            call (O(N) write). INSERT OR REPLACE touches a single B-tree page.
        SCHEMA: returns the composite key "space::node_id" for compatibility
            with LocalDocStore callers that use the return value.
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalSQLDocStore is not available.")
        updated_at = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO doc_nodes(space, node_id, node_type, properties, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (space, node_id, node_type, json.dumps(properties), updated_at),
            )
            self._conn.commit()
        return f"{space}::{node_id}"

    def get_node_doc(self, space: str, node_id: str) -> dict[str, Any] | None:
        """
        Replaces: LocalDocStore.get_node_doc / MongoStore.get_node_doc
        WHY SQLite: O(log N) PK lookup vs O(N) full JSON parse + dict.get().
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalSQLDocStore is not available.")
        row = self._conn.execute(
            "SELECT space, node_id, node_type, properties, updated_at"
            " FROM doc_nodes WHERE space=? AND node_id=?",
            (space, node_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_node(row)

    def list_nodes(
        self, space: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        Replaces: LocalDocStore.list_nodes / MongoStore.list_nodes
        WHY SQLite: this is the hot path — BM25 cache rebuild calls
            list_nodes(limit=50000) on every query. JSON backend loads and
            parses the entire file then slices; SQLite uses LIMIT in the query
            so only the required rows are read from disk.
        SCHEMA: space=None skips the WHERE clause entirely (no filter cost).
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalSQLDocStore is not available.")
        if space:
            rows = self._conn.execute(
                "SELECT space, node_id, node_type, properties, updated_at"
                " FROM doc_nodes WHERE space=? LIMIT ?",
                (space, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT space, node_id, node_type, properties, updated_at"
                " FROM doc_nodes LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def delete_node_doc(self, space: str, node_id: str) -> bool:
        """
        Replaces: LocalDocStore.delete_node_doc / MongoStore.delete_node_doc
        WHY SQLite: JSON backend loads the whole file, removes the key, then
            re-serializes — O(N). SQLite DELETE by PK is O(log N).
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalSQLDocStore is not available.")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM doc_nodes WHERE space=? AND node_id=?",
                (space, node_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Source ingestion
    # ------------------------------------------------------------------

    def upsert_source(
        self, source_id: str, text: str, metadata: dict[str, Any]
    ) -> str:
        """
        Replaces: LocalDocStore.upsert_source / MongoStore.upsert_source
        WHY SQLite: same O(N) → O(log N) argument as upsert_node_doc.
        SCHEMA: text is stored as-is (no truncation); callers that need
            truncation should do so before calling (MongoStore doesn't truncate
            either — only LocalDocStore did for legacy reasons).
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalSQLDocStore is not available.")
        ingested_at = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO doc_sources(source_id, text, metadata, ingested_at)
                VALUES (?, ?, ?, ?)
                """,
                (source_id, text, json.dumps(metadata), ingested_at),
            )
            self._conn.commit()
        return source_id

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        """
        Replaces: LocalDocStore.get_source / MongoStore.get_source
        WHY SQLite: O(log N) PK lookup.
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalSQLDocStore is not available.")
        row = self._conn.execute(
            "SELECT source_id, text, metadata, ingested_at"
            " FROM doc_sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "source_id": row["source_id"],
            "text": row["text"],
            "metadata": json.loads(row["metadata"]),
            "ingested_at": row["ingested_at"],
        }

    def list_sources(self, limit: int = 100) -> list[dict[str, Any]]:
        """
        Replaces: LocalDocStore.list_sources / MongoStore.list_sources
        WHY SQLite: LIMIT in query avoids loading rows we discard.
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalSQLDocStore is not available.")
        rows = self._conn.execute(
            "SELECT source_id, text, metadata, ingested_at FROM doc_sources LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "source_id": r["source_id"],
                "text": r["text"],
                "metadata": json.loads(r["metadata"]),
                "ingested_at": r["ingested_at"],
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
        """
        Replaces: LocalDocStore.log_event / MongoStore.log_event
        WHY SQLite: JSON backend loads, appends, re-serializes the entire log
            on every event — O(N). SQLite INSERT is O(log N).
        SCHEMA: event_id uses uuid4 (vs LocalDocStore's composite string key)
            to guarantee uniqueness under concurrent writers and match
            MongoStore's ObjectId semantics.  Returns event_id so callers can
            correlate entries.
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalSQLDocStore is not available.")
        event_id = str(uuid.uuid4())
        timestamp = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_log(event_id, event_type, subject_id, details, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, event_type, subject_id, json.dumps(details), timestamp),
            )
            self._conn.commit()
        return event_id

    def get_audit_log(
        self, limit: int = 100, event_type: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Replaces: LocalDocStore.get_audit_log / MongoStore.get_audit_log
        WHY SQLite: LocalDocStore sorts all entries in Python then slices;
            SQLite uses the idx_audit_ts index to return top-N rows without
            sorting the whole table.
        SCHEMA: ORDER BY timestamp DESC mirrors MongoStore's sort([("timestamp", -1)]).
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalSQLDocStore is not available.")
        if event_type:
            rows = self._conn.execute(
                "SELECT event_id, event_type, subject_id, details, timestamp"
                " FROM audit_log WHERE event_type=?"
                " ORDER BY timestamp DESC LIMIT ?",
                (event_type, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT event_id, event_type, subject_id, details, timestamp"
                " FROM audit_log ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "event_id": r["event_id"],
                "event_type": r["event_type"],
                "subject_id": r["subject_id"],
                "details": json.loads(r["details"]),
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def collection_stats(self) -> dict[str, int]:
        """
        Replaces: LocalDocStore.collection_stats / MongoStore.collection_stats
        WHY SQLite: COUNT(*) uses the table's B-tree internal page count —
            O(1) for SQLite (no full scan). JSON backend calls len() on a
            freshly-loaded dict — O(N) just to count.
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalSQLDocStore is not available.")
        counts = {}
        for table, key in [
            ("doc_nodes", "nodes"),
            ("doc_sources", "sources"),
            ("audit_log", "audit_log"),
        ]:
            row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
            counts[key] = int(row[0]) if row else 0
        return counts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> dict[str, Any]:
        """Convert a doc_nodes DB row to the dict format callers expect."""
        return {
            "space": row["space"],
            "node_id": row["node_id"],
            "node_type": row["node_type"],
            "properties": json.loads(row["properties"]),
            "updated_at": row["updated_at"],
        }
