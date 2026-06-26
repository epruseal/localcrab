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

    Thread-safety: each thread gets its own sqlite3 connection (threading.local);
    sharing one connection across threads corrupts even reads. WAL lets per-thread
    connections read concurrently while a threading.Lock serialises writers.
    """

    def __init__(self, db_path: str) -> None:
        """
        Replaces: LocalDocStore.__init__(data_dir) / MongoStore.__init__(uri, db_name)
        WHY: receive a file path rather than a directory so the caller controls
             exactly where the DB lives (simpler than data_dir + filename logic).
        THREAD SAFETY: each thread gets its own sqlite3 connection
             (threading.local) because sharing one connection across threads
             corrupts even reads. A threading.Lock serialises writers so only one
             per-thread connection writes the WAL file at a time (avoids
             SQLITE_BUSY); reads take no lock and run concurrently under WAL.
        """
        self._db_path = db_path
        self._available = False
        self._fts_ok = False  # SQLite FTS5 키워드 색인 가용 여부(capability)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._conns_lock = threading.Lock()
        self._all_conns: list[sqlite3.Connection] = []
        self._init_db()

    def _new_conn(self) -> sqlite3.Connection:
        """이 스레드 전용 커넥션 생성. WAL + synchronous=NORMAL 근거는
        local_graph_store.py 와 동일(reader/writer 격리, 체크포인트시에만 fsync)."""
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection | None:
        """현재 스레드의 커넥션(없으면 생성). 기존 메서드의 self._conn.X 호출 호환."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        """
        Replaces: LocalDocStore's implicit dir creation + LocalGraphStore._init_db()
        WHY: WAL + synchronous=NORMAL is the same pattern as local_graph_store.py.
             Creates all three tables in a single transaction for atomicity.
        """
        try:
            conn = self._conn  # 이 스레드 커넥션 생성 + WAL pragma
            cur = conn.cursor()
            for ddl in _DDL:
                cur.execute(ddl)
            conn.commit()
            self._available = True
            logger.info("LocalSQLDocStore initialised at %s", self._db_path)
        except Exception as exc:
            logger.warning("LocalSQLDocStore init failed: %s", exc)
            self._available = False
            return
        # FTS5 키워드 색인(선택) — 빌드에 FTS5 모듈이 없으면 graceful 비활성.
        # 본문(doc_sources.text)을 한+영 unicode61 토크나이저로 색인 → 약어·표준번호·영어
        # 다중어 질의 정확매칭(하이브리드 키워드 레그). 미가용 시 supports_keyword=False.
        try:
            cur.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS doc_sources_fts USING fts5("
                "source_id UNINDEXED, text, "
                "tokenize='unicode61 remove_diacritics 0')"
            )
            # 최초 1회 마이그레이션(idempotent): FTS 비었고 본문이 있으면 일괄 색인.
            n_fts = cur.execute("SELECT count(*) FROM doc_sources_fts").fetchone()[0]
            n_src = cur.execute("SELECT count(*) FROM doc_sources").fetchone()[0]
            if n_fts == 0 and n_src > 0:
                cur.execute(
                    "INSERT INTO doc_sources_fts(source_id, text) "
                    "SELECT source_id, text FROM doc_sources"
                )
                logger.info("doc_sources_fts migrated %d rows", n_src)
            conn.commit()
            self._fts_ok = True
        except Exception as exc:
            logger.warning("FTS5 keyword index unavailable (graceful): %s", exc)
            self._fts_ok = False

    # ------------------------------------------------------------------
    # Availability / lifecycle
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Replaces: LocalDocStore.available / MongoStore.available"""
        return self._available

    @property
    def supports_keyword(self) -> bool:
        """키워드 전문검색(FTS5) 지원 여부 — 하이브리드 키워드 레그 capability.
        다른 백엔드(Mongo/pgvector)는 각자 이 capability를 구현/노출한다."""
        return self._available and self._fts_ok

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
        with self._conns_lock:
            for conn in self._all_conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_conns.clear()

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

    def bm25_fingerprint(self, limit: int = 50000) -> tuple[int, str]:
        """Cheap ``(count, max_updated_at)`` over the first ``limit`` nodes.

        Equivalent to ``compute_fingerprint(self.list_nodes(limit=limit))`` but
        WITHOUT parsing JSON ``properties`` — this is the query hot-path probe
        HybridQuery uses to detect a stale BM25 cache without the full 50k
        ``list_nodes`` scan. The ``LIMIT`` subquery mirrors ``list_nodes`` (same
        rowid order, same cap) so the count agrees even when the corpus exceeds
        ``limit`` — BM25Index only indexes that many rows. ``MAX(updated_at)`` is
        backed by ``idx_doc_nodes_updated``; ``doc_nodes`` has no ``ingested_at``
        column, so ``compute_fingerprint`` (which also checks ``ingested_at``)
        yields the same value here.
        """
        if not self._available or not self._conn:
            raise RuntimeError("LocalSQLDocStore is not available.")
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), '')"
            " FROM (SELECT updated_at FROM doc_nodes LIMIT ?)",
            (limit,),
        ).fetchone()
        return (int(row[0]), str(row[1]))

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
            # FTS5 동기화(delete+insert) — 본문 교체 시 옛 토큰 제거.
            if self._fts_ok:
                try:
                    self._conn.execute(
                        "DELETE FROM doc_sources_fts WHERE source_id=?", (source_id,)
                    )
                    self._conn.execute(
                        "INSERT INTO doc_sources_fts(source_id, text) VALUES (?, ?)",
                        (source_id, text),
                    )
                except Exception as exc:
                    logger.warning("FTS sync failed for %s: %s", source_id, exc)
            self._conn.commit()
        return source_id

    def keyword_search(
        self,
        query: str,
        pack_ids: list[str] | None = None,
        include_unpackaged: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """본문(doc_sources) FTS5 키워드 검색 — 하이브리드 키워드 레그.

        백엔드-중립 인터페이스: 호출측은 ``supports_keyword`` 로 가용성 확인 후 사용.
        질의는 \\w+ 토큰만 추출해 각 토큰을 따옴표로 감싸 OR 결합 → FTS5 연산자
        주입/구문오류 방지(따옴표·별표·연산자 입력도 안전). bm25 랭크 오름차순(=best first).
        반환: [{source_id, node_id, text, metadata, score}] (score 높을수록 우수).
        """
        if not self._available or not self._fts_ok or not self._conn:
            return []
        import re

        toks = re.findall(r"\w+", query or "", flags=re.UNICODE)
        if not toks:
            return []
        match = " OR ".join(f'"{t}"' for t in toks)
        try:
            rows = self._conn.execute(
                "SELECT f.source_id AS sid, s.text AS text, s.metadata AS meta, "
                "bm25(doc_sources_fts) AS rank "
                "FROM doc_sources_fts f JOIN doc_sources s ON s.source_id = f.source_id "
                "WHERE doc_sources_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, max(1, limit) * 5),  # pack 필터 대비 overfetch
            ).fetchall()
        except Exception as exc:
            logger.warning("keyword_search failed: %s", exc)
            return []
        try:
            from opencrab.ontology.pack_provenance import matches_pack_filter
        except Exception:
            matches_pack_filter = None  # type: ignore
        out: list[dict[str, Any]] = []
        for r in rows:
            meta = json.loads(r["meta"]) if r["meta"] else {}
            if matches_pack_filter is not None and not matches_pack_filter(
                {"metadata": meta}, pack_ids, include_unpackaged
            ):
                continue
            out.append({
                "source_id": r["sid"],
                "node_id": meta.get("node_id") or r["sid"],
                "text": r["text"],
                "metadata": meta,
                "score": -float(r["rank"] or 0.0),  # bm25: 작을수록 우수 → 부호반전
            })
            if len(out) >= limit:
                break
        return out

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
