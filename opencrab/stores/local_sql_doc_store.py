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

STAGE 6a (F1): the 13-method surface's SQL text and dict-shaping now live in
    ``_SqlDocStoreBase`` (``_sql_doc_base.py``), parameterised by the SQLite
    ``SqlDialect`` (``_sql_dialect.py``). This class supplies the SQLite-only
    pieces the base deliberately doesn't cover: connection management (via
    ``_SqliteConnMixin``), DDL bootstrap + FTS5 capability probing, the FTS5
    ``keyword_search`` implementation, and the FTS5 shadow-table sync that
    ``upsert_source`` needs on top of the base's ``doc_sources`` write.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from opencrab.stores._sql_dialect import SQLITE
from opencrab.stores._sql_doc_base import DOC_STORE_SCHEMA, _SqlDocStoreBase
from opencrab.stores._sqlite_base import _SqliteConnMixin

logger = logging.getLogger(__name__)


class LocalSQLDocStore(_SqliteConnMixin, _SqlDocStoreBase):
    """SQLite-backed document store with the same interface as MongoStore /
    LocalDocStore.

    All writes use INSERT ... ON CONFLICT DO UPDATE (UPSERT) so callers can call upsert_*
    methods unconditionally without managing existence checks.

    Thread-safety: each thread gets its own sqlite3 connection (threading.local);
    sharing one connection across threads corrupts even reads. WAL lets per-thread
    connections read concurrently while a threading.Lock serialises writers.
    """

    _dialect = SQLITE

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
        self._available = False
        self._fts_ok = False  # SQLite FTS5 키워드 색인 가용 여부(capability)
        self._init_conn_state(db_path)
        self._init_db()

    def _init_db(self) -> None:
        """
        Replaces: LocalDocStore's implicit dir creation + LocalGraphStore._init_db()
        WHY: WAL + synchronous=NORMAL is the same pattern as local_graph_store.py.
             DDL text now comes from DOC_STORE_SCHEMA via SQLITE.render_ddl() —
             same three tables/indexes, same statement order, dialect-shared
             with pg_doc_store.py — created in a single transaction for atomicity.
        """
        try:
            conn = self._conn  # 이 스레드 커넥션 생성 + WAL pragma
            cur = conn.cursor()
            for ddl in SQLITE.render_ddl(DOC_STORE_SCHEMA):
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

    # ------------------------------------------------------------------
    # Base-hook implementations (see _SqlDocStoreBase adoption contract)
    # ------------------------------------------------------------------

    def _table(self, name: str) -> str:
        return name

    def _fetch_all(self, sql: str, params: dict[str, Any]) -> list[Any]:
        return self._conn.execute(sql, params).fetchall()

    def _fetch_one(self, sql: str, params: dict[str, Any]) -> Any | None:
        return self._conn.execute(sql, params).fetchone()

    def _exec_write(self, sql: str, params: dict[str, Any]) -> int:
        with self._tx() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def _exec_write_many(self, statements: list[tuple[str, dict[str, Any]]]) -> list[int]:
        with self._tx() as conn:
            return [conn.execute(sql, params).rowcount for sql, params in statements]

    def _row_get(self, row: Any, name: str) -> Any:
        return row[name]

    # _require_available is provided by _SqliteConnMixin.

    # ------------------------------------------------------------------
    # Source ingestion — override to keep the FTS5 shadow table in sync
    # ------------------------------------------------------------------

    def upsert_source(
        self, source_id: str, text: str, metadata: dict[str, Any]
    ) -> str:
        """
        Replaces: LocalDocStore.upsert_source / MongoStore.upsert_source
        Writes doc_sources and syncs the FTS5 shadow table (delete+insert) in a
        SINGLE transaction (_exec_write_many), not via super().upsert_source()
        + a separate commit — otherwise doc_sources and doc_sources_fts commit
        independently and a failure between them (or a later exception) leaves
        the two tables permanently out of sync with no rollback able to fix it.
        """
        self._require_available()
        now = datetime.now(UTC)
        sql = self._dialect.upsert(
            self._table("doc_sources"),
            ["source_id", "text", "metadata", "ingested_at"],
            conflict_cols=["source_id"],
            update_cols=["text", "metadata", "ingested_at"],
            json_columns=["metadata"],
        )
        statements: list[tuple[str, dict[str, Any]]] = [
            (
                sql,
                {
                    "source_id": source_id,
                    "text": text,
                    "metadata": json.dumps(metadata),
                    "ingested_at": self._dialect.bind_value_for_timestamp(now),
                },
            )
        ]
        if self._fts_ok:
            statements.append(
                ("DELETE FROM doc_sources_fts WHERE source_id=:source_id", {"source_id": source_id})
            )
            statements.append(
                (
                    "INSERT INTO doc_sources_fts(source_id, text) VALUES (:source_id, :text)",
                    {"source_id": source_id, "text": text},
                )
            )
        self._exec_write_many(statements)
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
