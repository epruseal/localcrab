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
    Round-tripping a whole JSON column stays caller-side (json.loads) rather
    than in SQL. That does NOT make this store free of SQL JSON functions:
    keyword_search()'s space and pack filters, and the shared base's scope
    predicates, go through SqlDialect.json_get / json_truthy_text, which emit
    json_extract() on SQLite. So this store requires a build with the JSON
    functions enabled -- availability is a build option, not a version -- on
    top of the 3.24.0 floor from the shared upsert path (see pyproject.toml's
    Runtime SQLite version note).

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
import time
from typing import Any

from opencrab.locking import StoreBootstrapLockTimeoutError
from opencrab.stores import _node_bm25_cache as _ncache
from opencrab.stores import _node_bm25_sqlite as _nbm
from opencrab.stores import _source_fts_sqlite as _sfts
from opencrab.stores._graph_common import decode_properties
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

    def __init__(self, db_path: str, *, shared: Any = None) -> None:
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
        self._node_bm25_ok = False  # #541 node BM25 index objects exist
        # #667: the posting cache and its version token. Both stay None when the cache is
        # off or the node index is unsupported. The switch is read once, here.
        self._bm25_cache: _ncache.PostingCache | None = None
        self._bm25_token: _ncache.TokenSource | None = None
        self._init_conn_state(db_path, shared)
        try:
            self._init_db()
            if self.supports_node_bm25 and _ncache.enabled():
                # Opened now, not at the first query, so that a replaced database file is seen.
                self._bm25_token = _ncache.TokenSource(self._db_path, self._new_conn)
                self._bm25_cache = _ncache.PostingCache()
        except BaseException as exc:
            # _init_db와 캐시 생성의 실패가 생성자 밖으로 나가면 등록한 공유 소유자를
            # 풀지 않아 수명 잠금이 남는다(#671). 잠금 시간 초과는 _init_db가 이미 닫고
            # 올렸으므로 다시 닫지 않는다(#578).
            if not isinstance(exc, StoreBootstrapLockTimeoutError):
                self.close()
            raise

    def close(self) -> None:
        # #667: ``close`` also runs from inside ``_init_db`` (bootstrap lock timeout), before
        # the token source exists.
        token = getattr(self, "_bm25_token", None)
        if token is not None:
            token.close()
        super().close()

    def _init_db(self) -> None:
        """
        Replaces: LocalDocStore's implicit dir creation + LocalGraphStore._init_db()
        WHY: WAL + synchronous=NORMAL is the same pattern as local_graph_store.py.
             DDL text now comes from DOC_STORE_SCHEMA via SQLITE.render_ddl() —
             same three tables/indexes, same statement order, dialect-shared
             with pg_doc_store.py — created in a single transaction for atomicity.
        """
        # 핵심 DDL(본체 3테이블)은 _tx() 없이 conn.commit() 그대로 둔다: 실패하면
        # _available=False 로 남고 이후 모든 쓰기는 _require_available()에서 막혀
        # _tx()까지 도달하지 못하므로, 여기서 미완료로 남는 부분 실행분이 나중
        # 커밋에 묻어 들어갈 경로 자체가 없다(#79 재검증에서 확인된 구분점).
        # issue #141 항목 2: 핵심 DDL과 FTS5 백필 전체를 write.lock 으로
        # 감싼다 — 재진입 가능하므로 이미 락을 쥔 진입점 안에서도 안전.
        #
        # codex 리뷰(PR #323, 회귀 게이트에서 발견): _bootstrap_lock() 자체의
        # 획득 실패(예: 존재하지 않는 부모 디렉터리에서 락 파일 경로를 만들려다
        # 나는 OSError)를 이 바깥 try 하나로 잡는다 — 안쪽 두 try(핵심 DDL,
        # FTS5)는 각자 자기 예외를 이미 삼키므로 이 바깥 except 에는 락 획득
        # 실패만 걸린다. FTS5 블록은 (양쪽 검증자가 지적한 대로) 반드시
        # with 본문 "안"에 있어야 한다 — 밖으로 나가면 백필 INSERT 가
        # write.lock 없이 돌아 항목2 가 막으려던 프로세스 간 TOCTOU 가
        # 되돌아온다. 획득 시간 초과(StoreBootstrapLockTimeoutError)는 이
        # 삼킴에서 빠진다: 아래 전용 except 가 닫고 전파한다(#578).
        try:
            with self._bootstrap_lock():
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
                if self._shared_guarded():
                    # #671: 게시 DB는 pack_delete_operations를 가져 작업 단위 밖 _tx()가 거부된다.
                    # 아래 쓰기 초기화는 그 거부를 삼켜 FTS와 node BM25를 조용히 끈다. 이 경우에는
                    # 쓰기 없이 객체 존재와 형식만 읽어 두 값을 정한다.
                    self._fts_ok = self._read_only_fts_ok(conn)
                    self._node_bm25_ok = self._read_only_node_bm25_ok(conn)
                    return
                # FTS5 키워드 색인(선택) — 빌드에 FTS5 모듈이 없으면 graceful 비활성.
                # 본문(doc_sources.text)을 한+영 unicode61 토크나이저로 색인 → 약어·표준번호·영어
                # 다중어 질의 정확매칭(하이브리드 키워드 레그). 미가용 시 supports_keyword=False.
                #
                # 핵심 DDL과 달리 이 블록은 실패해도 store가 available인 채로 남는다
                # (_fts_ok=False일 뿐) — 즉 이후 정상 _tx() 쓰기가 실제로 일어난다.
                # CREATE VIRTUAL TABLE 자체는 DDL이라 파이썬 sqlite3의 암묵적 BEGIN 대상이
                # 아니라 즉시 개별 커밋되며(IF NOT EXISTS라 멱등이므로 무해), 여기서 막는
                # 대상은 그다음 백필 INSERT(DML)다: 그 INSERT가 실패하면 sqlite3는 그
                # 문장이 시작한 트랜잭션을 커밋도 롤백도 하지 않은 채 커넥션에 열어 두고,
                # _tx() 없이 그냥 삼키면 그 열린 트랜잭션이 바로 다음 정상 쓰기의 commit()에
                # 얹혀 확정된다 — 이 이슈가 말하는 결함 그대로. _tx()는 실패 즉시
                # rollback()을 호출해 그 열린 트랜잭션을 정리한다.
                try:
                    with self._tx() as tx_conn:
                        cur = tx_conn.cursor()
                        cur.execute(
                            "CREATE VIRTUAL TABLE IF NOT EXISTS doc_sources_fts USING fts5("
                            "source_id UNINDEXED, text, "
                            "tokenize='unicode61 remove_diacritics 0')"
                        )
                        # #642: source_id lookup index on the FTS5 content table.
                        # CREATE INDEX is DDL, so it commits on its own and stays
                        # if a later statement of this block fails.
                        _sfts.require_layout(cur)
                        cur.execute(_sfts.INDEX_DDL)
                        # 최초 1회 마이그레이션(idempotent): FTS 비었고 본문이 있으면 일괄 색인.
                        n_fts = cur.execute("SELECT count(*) FROM doc_sources_fts").fetchone()[0]
                        n_src = cur.execute("SELECT count(*) FROM doc_sources").fetchone()[0]
                        if n_fts == 0 and n_src > 0:
                            cur.execute(
                                "INSERT INTO doc_sources_fts(source_id, text) "
                                "SELECT source_id, text FROM doc_sources"
                            )
                            logger.info("doc_sources_fts migrated %d rows", n_src)
                    self._fts_ok = True
                except Exception as exc:
                    logger.warning("FTS5 keyword index unavailable (graceful): %s", exc)
                    self._fts_ok = False
                self._init_node_bm25()
        except StoreBootstrapLockTimeoutError:
            # #578: propagate a busy write.lock instead of degrading (see
            # LocalGraphStore._init_db).
            self._available = False
            self.close()
            raise
        except Exception as exc:
            logger.warning("LocalSQLDocStore bootstrap lock failed: %s", exc)
            self._available = False
            return

    def _init_node_bm25(self) -> None:
        """#541: create or check the node BM25 index in one transaction.

        Runs under the bootstrap write.lock. On any failure the transaction
        rolls back, no index object stays, and the index is unsupported for
        this process (design 5.2).
        """
        from opencrab.ontology.bm25 import max_text_chars_setting

        try:
            with self._tx(immediate=True) as conn:
                state = _nbm.ensure_schema(conn, max_text_chars_setting())
            self._node_bm25_ok = True
            logger.info("node BM25 index state: %s", state)
        except Exception as exc:
            logger.warning("node BM25 index unavailable (graceful): %s", exc)
            self._node_bm25_ok = False

    def _shared_guarded(self) -> bool:
        """공유 모드이고 pack_delete_operations 표가 있는지. ``_tx()``의 보호와 같은 질의다."""
        if self._shared is None:
            return False
        return self._conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='pack_delete_operations'"
        ).fetchone() is not None

    def _read_only_fts_ok(self, conn: Any) -> bool:
        """쓰기 없이 doc FTS 객체 존재와 배치를 확인한다(#671). 색인 생성과 백필은 하지 않는다."""
        try:
            present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
            if not {_sfts.FTS, _sfts.CONTENT, _sfts.INDEX} <= present or not _sfts.layout_ok(conn):
                logger.warning("FTS5 keyword index unavailable (read-only check): objects missing or layout differs")
                return False
            return True
        except Exception as exc:  # noqa: BLE001 -- graceful off, same as the write path
            logger.warning("FTS5 keyword index unavailable (read-only check): %s", exc)
            return False

    def _read_only_node_bm25_ok(self, conn: Any) -> bool:
        """쓰기 없이 node BM25 객체 존재, 형식, ready 정합을 확인한다(#671).

        쓰기 모드의 ``ensure_schema``는 형식이나 정합이 어긋나면 색인을 비우고 backfill로
        되돌린다. 그것은 쓰기이므로 여기서는 하지 않고 기능을 끈다. state가 backfill이면 참이다.
        """
        from opencrab.ontology.bm25 import max_text_chars_setting

        try:
            present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
            required = {
                _nbm.META, _nbm.STATS, _nbm.DOCS, _nbm.PENDING, _nbm.FTS, _nbm.VROW, _nbm.VINST,
                *_nbm.TRIGGERS,
            }
            if not required <= present:
                logger.warning("node BM25 index unavailable (read-only check): objects missing")
                return False
            meta = _nbm._meta(conn)
            if (
                meta.get("format") != _nbm.FORMAT
                or _nbm._decode_chars(meta.get("max_text_chars")) != max_text_chars_setting()
            ):
                logger.warning("node BM25 index unavailable (read-only check): format or MAX_NODE_CHARS differs")
                return False
            if meta.get("state") == _nbm.STATE_READY:
                if conn.execute(f"SELECT 1 FROM {_nbm.PENDING} LIMIT 1").fetchone() is None:
                    rows = conn.execute("SELECT COUNT(*) FROM doc_nodes").fetchone()[0]
                    indexed = conn.execute(f"SELECT doc_count FROM {_nbm.STATS} WHERE id=1").fetchone()
                    if indexed is None or int(indexed[0]) != int(rows):
                        logger.warning("node BM25 index unavailable (read-only check): row count differs")
                        return False
            return True
        except Exception as exc:  # noqa: BLE001 -- graceful off, same as the write path
            logger.warning("node BM25 index unavailable (read-only check): %s", exc)
            return False

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
        if self._shared is not None:
            marker = self._shared.connection().execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pack_delete_operations'"
            ).fetchone()
            if marker is not None:
                if self._shared.in_unit_of_work:
                    self._shared.mark_rollback_only()
                raise RuntimeError("unscoped document SQL write is not supported")
        with self._tx() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def _exec_write_many(self, statements: list[tuple[str, dict[str, Any]]]) -> list[int]:
        if self._shared is not None:
            marker = self._shared.connection().execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pack_delete_operations'"
            ).fetchone()
            if marker is not None:
                if self._shared.in_unit_of_work:
                    self._shared.mark_rollback_only()
                raise RuntimeError("unscoped document SQL write is not supported")
        return self._exec_scoped_write_many(statements)

    def _exec_scoped_write_many(self, statements: list[tuple[str, dict[str, Any]]]) -> list[int]:
        with self._tx() as conn:
            return [conn.execute(sql, params).rowcount for sql, params in statements]

    def _row_get(self, row: Any, name: str) -> Any:
        return row[name]

    # _require_available is provided by _SqliteConnMixin.

    # ------------------------------------------------------------------
    # #541 node BM25 index (docs/issue-541-db-bm25.md)
    # ------------------------------------------------------------------

    # Search-time drain budget and lock wait. Operational values only.
    _NODE_BM25_DRAIN_BATCH = 128
    _NODE_BM25_DRAIN_SECONDS = 0.25
    _NODE_BM25_DRAIN_BUSY_MS = 50
    _NODE_BM25_BACKFILL_BATCH = 500

    @property
    def supports_node_bm25(self) -> bool:
        return bool(self._available and self._node_bm25_ok)

    def _exec_node_write(self, sql: str, params: dict[str, Any], space: str, node_id: str) -> int:
        """문서와 해당 BM25 키를 같은 트랜잭션에서 쓴다.

        일반 색인 실패는 색인 저장점만 되돌린다. 단일 모드 보호 실패는 전체 작업 단위를
        롤백한다. 기반 문서가 없는 삭제는 자기 팩 매핑과 대기 키도 보존한다.
        """
        with self._tx() as conn:
            requested_pack = self._requested_pack((space, node_id))
            if requested_pack is not None:
                try:
                    old = conn.execute(
                        "SELECT properties FROM main.doc_nodes WHERE space=? AND node_id=?",
                        (space, node_id),
                    ).fetchone()
                    if old is not None:
                        self._require_owner(old[0], requested_pack, (space, node_id))
                    if "properties" in params:
                        self._require_owner(params["properties"], requested_pack, (space, node_id))
                    _nbm.require_mapping_owner(conn, space, node_id, requested_pack)
                    if old is None and "properties" not in params:
                        return 0
                except Exception as exc:
                    self._reject_derived((space, node_id), exc)
            rowcount = conn.execute(sql, params).rowcount
            if self._node_bm25_ok:
                conn.execute("SAVEPOINT node_bm25")
                try:
                    _nbm.reindex_key(
                        conn, self._row_to_node, space, node_id, requested_pack=requested_pack
                    )
                except _sfts.DerivedRowProtectionError as exc:
                    self._reject_derived((space, node_id), exc)
                except Exception as exc:  # noqa: BLE001 -- 일반 색인 실패는 문서를 보존한다.
                    conn.execute("ROLLBACK TO SAVEPOINT node_bm25")
                    logger.warning("node BM25 index sync failed for one key: %s", exc)
                conn.execute("RELEASE SAVEPOINT node_bm25")
            return rowcount

    def node_bm25_ready(self) -> bool:
        """True when the index is ready in this code's format."""
        if not self.supports_node_bm25:
            return False
        try:
            return _nbm.is_ready(self._conn)
        except Exception as exc:  # noqa: BLE001 -- dropped tables, locked file
            logger.debug("node BM25 status read failed: %s", exc)
            return False

    def _node_bm25_try_drain(self) -> None:
        """Drain pending keys without waiting for locks (design 4.6)."""
        if self._shared is not None and self._shared.in_unit_of_work:
            # #671: inside a unit of work this thread already holds the writer lock and an open
            # transaction. A drain here would BEGIN a nested transaction and roll the unit back.
            return
        conn = self._conn
        try:
            if not _nbm.has_pending(conn):
                return
        except Exception as exc:  # noqa: BLE001 -- e.g. the index was dropped; the snapshot decides
            logger.debug("node BM25 pending check failed: %s", exc)
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            old_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            conn.execute(f"PRAGMA busy_timeout={int(self._NODE_BM25_DRAIN_BUSY_MS)}")
            try:
                deadline = time.monotonic() + self._NODE_BM25_DRAIN_SECONDS
                while time.monotonic() < deadline:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        done = _nbm.drain(conn, self._row_to_node, self._NODE_BM25_DRAIN_BATCH)
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                    if done < self._NODE_BM25_DRAIN_BATCH:
                        break
            finally:
                self._restore_busy_timeout(conn, old_timeout)
        except Exception as exc:  # noqa: BLE001 -- busy, read-only file, format change
            logger.debug("node BM25 search-time drain skipped: %s", exc)
        finally:
            self._lock.release()

    def node_bm25_search(
        self,
        query: str,
        *,
        pack_ids: list[str],
        spaces: list[str] | None = None,
        limit: int = 10,
        per_pack: bool = False,
    ) -> tuple[Any, list[str]] | None:
        """Search the node index. Returns ``None`` when it is not ready.

        Returns ``(hits, warnings)``; ``hits`` is a dict keyed by pack when
        ``per_pack``. The shape of each hit is the ``BM25Index.search`` shape.
        """
        if not self.supports_node_bm25:
            return None
        self._node_bm25_try_drain()
        conn = self._conn
        own = not conn.in_transaction
        # #667: read the version token before BEGIN. A caller that already holds a
        # transaction has a snapshot of unknown age, so it gets no cache.
        scope = None
        if own and self._bm25_cache is not None and self._bm25_token is not None:
            scope = _ncache.Scope.before_begin(self._bm25_cache, self._bm25_token)
        if own:
            conn.execute("BEGIN")
        try:
            return _nbm.search(
                conn, self._row_to_node, query,
                pack_ids=pack_ids, spaces=spaces, limit=limit, per_pack=per_pack,
                cache_scope=scope,
            )
        except _nbm.NotReadyError:
            return None
        finally:
            if own:
                conn.commit()

    def node_bm25_sync_pending(self, *, max_seconds: float | None = None) -> int:
        """Drain pending keys (waiting for locks). Returns the remaining count."""
        deadline = None if max_seconds is None else time.monotonic() + max_seconds
        while True:
            with self._tx(immediate=True) as conn:
                done = _nbm.drain(conn, self._row_to_node, self._NODE_BM25_BACKFILL_BATCH)
            if done == 0 or (deadline is not None and time.monotonic() >= deadline):
                break
        return _nbm.pending_count(self._conn)

    def node_bm25_backfill_step(self, batch: int | None = None) -> dict[str, Any]:
        with self._tx(immediate=True) as conn:
            return _nbm.backfill_step(
                conn, self._row_to_node, batch or self._NODE_BM25_BACKFILL_BATCH
            )

    def node_bm25_status(self) -> dict[str, Any]:
        if not self.supports_node_bm25:
            return {"state": "unsupported"}
        return _nbm.status(self._conn)

    def node_bm25_verify(self) -> dict[str, Any]:
        if self._shared is not None and self._shared.in_unit_of_work:
            raise RuntimeError("BM25 verification cannot run inside a shared unit of work")
        conn = self._conn
        conn.execute("BEGIN")
        try:
            return _nbm.verify(conn, self._row_to_node)
        finally:
            conn.commit()

    def node_bm25_drop(self) -> None:
        with self._bootstrap_lock(), self._tx(immediate=True) as conn:
            _nbm.drop(conn)
        self._node_bm25_ok = False

    # ------------------------------------------------------------------
    # Source ingestion — override to keep the FTS5 shadow table in sync
    # ------------------------------------------------------------------

    def log_event(self, event_type: str, subject_id: str | None, details: dict[str, Any]) -> str:
        if self._shared is None:
            return super().log_event(event_type, subject_id, details)
        self._require_available()
        import contextlib
        import os
        import uuid
        from datetime import UTC, datetime

        from opencrab.stores._json import dump_props

        event_id = str(uuid.uuid4())
        from opencrab.locking import write_lock

        transaction = self._shared.joined_tx() if self._shared.in_unit_of_work else self._shared.unit_of_work()
        lock = contextlib.nullcontext() if self._shared.in_unit_of_work else write_lock(
            os.path.dirname(self._shared.db_path)
        )
        with lock, transaction as conn:
            conn.execute(
                "INSERT INTO audit_log (event_id, event_type, subject_id, details, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_id, event_type, subject_id, dump_props(details), datetime.now(UTC).isoformat()),
            )
        return event_id

    def _reject_derived(self, identity: Any, reason: Exception) -> None:
        logger.warning("single SQLite derived protection rejected %r: %s", identity, reason)
        self._shared.mark_rollback_only()
        raise _sfts.DerivedRowProtectionError(f"{identity!r}: {reason}") from reason

    def _requested_pack(self, identity: Any) -> str | None:
        if self._shared is None or not self._shared.in_unit_of_work or not self._shared_guarded():
            return None
        validated = self._shared._uow.validated_packs
        requested = getattr(self._shared._uow, "requested_pack", None)
        if requested is None and len(validated) == 1:
            requested = next(iter(validated))
        if not isinstance(requested, str) or not requested or requested not in validated:
            self._reject_derived(identity, RuntimeError("request pack is not validated"))
        return requested

    def _require_owner(self, raw: Any, requested: str, identity: Any, *, source: bool = False) -> None:
        try:
            properties, corrupted = decode_properties(raw)
            owner = properties.get("pack_id")
            if source and (
                owner is None or owner is False or owner == ""
                or (isinstance(owner, (int, float)) and not isinstance(owner, bool) and owner == 0)
            ):
                owner = properties.get("source")
            if corrupted or not isinstance(owner, str) or owner != requested:
                raise RuntimeError("stored owner differs from request pack")
        except Exception as exc:
            self._reject_derived(identity, exc)

    def _source_guard(self, conn: Any, source_id: str, metadata: Any = None, *, writing: bool = False) -> bool:
        """동일 쓰기 단위에서 소유와 기반 행 없는 파생 행을 확인한다."""
        requested = self._requested_pack(source_id)
        row = conn.execute("SELECT metadata FROM main.doc_sources WHERE source_id=?", (source_id,)).fetchone()
        if writing:
            self._require_owner(metadata, requested, source_id, source=True)
        if row is not None:
            self._require_owner(row[0], requested, source_id, source=True)
            return True
        derived = conn.execute(
            "SELECT 1 FROM main.doc_source_integrity WHERE source_id=?", (source_id,)
        ).fetchone() or conn.execute(
            f"SELECT 1 FROM main.{_sfts.CONTENT} WHERE c0=? LIMIT 1", (source_id,)
        ).fetchone()
        if derived:
            reason = RuntimeError("source base row is absent; derived rows remain")
            if writing:
                self._reject_derived(source_id, reason)
            logger.warning("single SQLite source %r preserved: %s", source_id, reason)
        return False

    def _write_source_state(
        self,
        source_id: str,
        text: str,
        metadata: dict[str, Any],
        source_integrity: dict[str, str | int],
    ) -> str:
        """Write source state and the FTS shadow entry in one transaction."""
        if self._shared_guarded():
            with self._tx() as conn:
                try:
                    self._source_guard(conn, source_id, metadata, writing=True)
                    source_params, integrity_params = self._source_write_params(
                        source_id, text, metadata, source_integrity
                    )
                    if self._fts_ok:
                        _sfts.delete_protected(conn, source_id)
                    conn.execute(self._source_upsert_sql(), source_params)
                    conn.execute(self._source_integrity_upsert_sql(), integrity_params)
                    if self._fts_ok:
                        _sfts.insert_protected(conn, source_id, text)
                except Exception as exc:
                    self._reject_derived(source_id, exc)
            return source_id
        source_params, integrity_params = self._source_write_params(
            source_id, text, metadata, source_integrity
        )
        statements: list[tuple[str, dict[str, Any]]] = [
            (self._source_upsert_sql(), source_params),
            (self._source_integrity_upsert_sql(), integrity_params),
        ]
        if self._fts_ok:
            statements.extend(
                [
                    (_sfts.delete_sql("= :source_id"), {"source_id": source_id}),
                    (
                        "INSERT INTO doc_sources_fts(source_id, text) VALUES (:source_id, :text)",
                        {"source_id": source_id, "text": text},
                    ),
                ]
            )
        self._exec_scoped_write_many(statements)
        return source_id

    def upsert_source(
        self, source_id: str, text: str, metadata: dict[str, Any]
    ) -> str:
        """Write original source content with its FTS entry atomically."""
        from opencrab.stores._source_integrity import current_source_copy_state

        state = current_source_copy_state()
        if state is None:
            return self.write_source_original(source_id, text, metadata)
        return self.write_source_copy(source_id, text, metadata, state)

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        if not self._shared_guarded():
            return super().get_source(source_id)
        from opencrab.common.graph_identity import (
            GraphPropertyValidationError,
            PropertyDecodeDepthError,
            parse_properties_object,
        )

        self._require_available()
        row = self._fetch_one(
            "SELECT source_id, text, metadata, ingested_at FROM main.doc_sources WHERE source_id=:source_id",
            {"source_id": source_id},
        )
        if row is None:
            return None
        try:
            metadata = parse_properties_object(self._row_get(row, "metadata"))
        except PropertyDecodeDepthError:
            raise
        except GraphPropertyValidationError:
            return self._row_to_source(row)
        from opencrab.stores._sql_doc_base import _ts_str

        return {
            "source_id": self._row_get(row, "source_id"),
            "text": self._row_get(row, "text"),
            "metadata": metadata,
            "ingested_at": _ts_str(self._row_get(row, "ingested_at")),
        }

    def rewrite_source_metadata(self, source_id, metadata, source_state):
        if not self._shared_guarded():
            return super().rewrite_source_metadata(source_id, metadata, source_state)
        with self._tx(immediate=True) as conn:
            try:
                self._source_guard(conn, source_id, metadata, writing=True)
                row = conn.execute(
                    self._source_state_join("s.source_id=:source_id"), {"source_id": source_id}
                ).fetchone()
                current = self._source_state_from_row(row) if row is not None else None
                if current is None or current != source_state:
                    raise ValueError("source state changed before metadata rewrite")
                return self.write_source_copy(source_id, current.text, metadata, current)
            except Exception as exc:
                self._reject_derived(source_id, exc)

    def delete_sources_with_state(self, source_ids: list[str]) -> int:
        if not source_ids:
            return 0
        if self._shared_guarded():
            with self._tx() as conn:
                count = 0
                for source_id in source_ids:
                    try:
                        if not self._source_guard(conn, source_id):
                            continue
                        if self._fts_ok:
                            _sfts.delete_protected(conn, source_id)
                        conn.execute("DELETE FROM main.doc_source_integrity WHERE source_id=?", (source_id,))
                        count += conn.execute(
                            "DELETE FROM main.doc_sources WHERE source_id=?", (source_id,)
                        ).rowcount
                    except Exception as exc:
                        self._reject_derived(source_id, exc)
            return count
        placeholders = ", ".join(f":sid_{index}" for index in range(len(source_ids)))
        params = {f"sid_{index}": source_id for index, source_id in enumerate(source_ids)}
        statements = [
            (
                f"DELETE FROM {self._table('doc_sources')} WHERE source_id IN ({placeholders})",
                params,
            ),
            (
                f"DELETE FROM {self._table('doc_source_integrity')} "
                f"WHERE source_id IN ({placeholders})",
                params,
            ),
        ]
        if self._fts_ok:
            statements.append((_sfts.delete_sql(f"IN ({placeholders})"), params))
        return self._exec_scoped_write_many(statements)[0]

    def source_fts_report(self) -> dict[str, Any]:
        """#642: counts of the source FTS rows from one read snapshot."""
        self._require_available()
        return _sfts.report(self._conn)

    def prune_orphan_source_fts(self) -> dict[str, Any]:
        """#642: delete the FTS rows that have no doc_sources row.

        One write transaction finds and deletes the rows. Only the operator
        command ``packs source-fts-orphans --apply`` calls this method, under
        write.lock.
        """
        self._require_available()
        if not self._fts_ok:
            raise RuntimeError("the source FTS index is not available")
        with self._tx(immediate=True) as conn:
            return _sfts.prune_orphans(conn)

    def keyword_search(
        self,
        query: str,
        *,
        pack_ids: list[str],
        include_unpackaged: bool = False,
        limit: int = 20,
        spaces: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """본문(doc_sources) FTS5 키워드 검색 — 하이브리드 키워드 레그.

        백엔드-중립 인터페이스: 호출측은 ``supports_keyword`` 로 가용성 확인 후 사용.
        질의는 \\w+ 토큰만 추출해 각 토큰을 따옴표로 감싸 OR 결합 → FTS5 연산자
        주입/구문오류 방지(따옴표·별표·연산자 입력도 안전). bm25 랭크 오름차순(=best first).
        반환: [{source_id, node_id, text, metadata, score}] (score 높을수록 우수).

        ``spaces`` (issue #52): strict space-membership filter pushed into
        the SQL WHERE clause (``json_extract(s.metadata, '$.space')``, via
        ``self._dialect.json_get`` — the same extractor the graph store's
        pack filter uses), not a Python post-filter.

        PACK FILTER (issue #147 §3.6, rewritten from a Python post-filter to
        SQL): the OLD implementation lazily imported
        ``opencrab.ontology.pack_provenance.matches_pack_filter`` inside this
        method, applied it AFTER an overfetch (``max(1, limit) * 5`` rows),
        and treated an import failure as "no filter" (``matches_pack_filter
        = None`` → every overfetched row passed) -- a fail-OPEN fallback on
        top of a filter (``matches_pack_filter``/``infer_pack_id``) that
        also inferred ``pack_id`` from ``source_path``/``source_id`` regex
        matches, which is spoofable by anything that can set its own
        metadata. Both are gone: the predicate is now
        ``json_truthy_text(s.metadata,'pack_id') IN <array bind>`` (via
        ``self._dialect.in_string_array`` -- ``_sql_dialect.py``), pushed
        into the SAME WHERE clause as ``spaces``, ahead of ``LIMIT`` -- so
        the ``* 5`` overfetch is also gone: every row that reaches ``LIMIT``
        already matched every filter, nothing needs a second Python pass.

        ``pack_ids`` has NO DEFAULT (every authorized caller must supply a
        concrete scope) -- empty ``pack_ids`` returns ``[]`` WITHOUT
        querying, never "everything". ``limit <= 0`` (issue #139, same
        contract as ``_graph_protocol.py``'s ``export_nodes``, issue #120):
        returns ``[]`` WITHOUT querying, checked ahead of the ``self._conn``
        access below so a fresh worker thread's first call does not trigger
        ``_new_conn()``'s ``PRAGMA`` side effects before the guard fires.
        ``include_unpackaged`` is ACCEPTED but
        IGNORED under this predicate: ``json_truthy_text`` returns SQL NULL
        for a missing/falsy pack_id, and NULL never satisfies ``IN``
        membership on any dialect -- there is no "OR pack_id IS NULL" branch
        to gate, because an authorized read-scope caller must never see
        unpackaged rows regardless (invariant 5; the old post-filter's
        escape hatch has no equivalent once that post-filter is gone). Kept
        in the signature only so an existing caller passing it does not
        immediately ``TypeError``.

        ERROR PROPAGATION (issue #147 §3.4(c)): unlike the old version, a
        failure in this query is NOT swallowed. The old broad
        ``except Exception: logger.warning(...); return []`` existed for a
        malformed FTS5 MATCH expression, but the MATCH text here is built
        exclusively from ``\\w+``-only tokens (see above) -- it can never
        contain an FTS5 operator or unescaped quote, so that failure mode
        was never actually reachable through this method's public
        parameters. Removing the swallow means a genuine pack-predicate/bind
        error (a bug) surfaces as an exception instead of silently becoming
        an empty search leg -- the same class of "oh no exception → 0
        results, unnoticed" swallow issue #147 §3.4(c) closes for the
        vector/graph legs.
        """
        if not self._available or not self._fts_ok:
            return []
        if not pack_ids or limit <= 0:
            return []
        if not self._conn:
            return []
        built = self._keyword_where(query, pack_ids, spaces)
        if built is None:
            return []
        where_sql, params, _pack_expr = built
        params.append(limit)  # limit<=0 already guarded above; always positive here
        # Equal ranks keep FTS scan order (SQLite leaves it unspecified, but it is
        # stable; keyword_search_per_pack relies on the same order, see its tests).
        rows = self._conn.execute(
            "SELECT f.source_id AS sid, s.text AS text, s.metadata AS meta, "
            "bm25(doc_sources_fts) AS rank "
            "FROM doc_sources_fts f JOIN doc_sources s ON s.source_id = f.source_id "
            f"{where_sql} ORDER BY rank LIMIT ?",
            params,
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            meta = json.loads(r["meta"]) if r["meta"] else {}
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

    def _keyword_where(
        self, query: str, pack_ids: list[str], spaces: list[str] | None
    ) -> tuple[str, list[Any], str] | None:
        """Shared WHERE clause of the FTS keyword queries, or ``None`` when
        the query has no ``\\w+`` token. Returns ``(where_sql, params,
        pack_expr)``; the caller appends its own trailing params."""
        import re

        toks = re.findall(r"\w+", query or "", flags=re.UNICODE)
        if not toks:
            return None
        match = " OR ".join(f'"{t}"' for t in toks)
        where_sql = "WHERE doc_sources_fts MATCH ?"
        params: list[Any] = [match]
        if spaces:
            # issue #415: json_get_safe -- one syntactically malformed
            # metadata row must not crash this whole keyword-search leg.
            space_expr = self._dialect.json_get_safe("s.metadata", "space")
            placeholders = ",".join("?" for _ in spaces)
            where_sql += f" AND {space_expr} IN ({placeholders})"
            params.extend(spaces)
        pack_expr = self._dialect.json_truthy_text("s.metadata", "pack_id")
        pack_frag, transform = self._dialect.in_string_array(pack_expr, "?")
        where_sql += f" AND {pack_frag}"
        params.append(transform(sorted(set(pack_ids))))
        return where_sql, params, pack_expr

    def keyword_search_per_pack(
        self,
        query: str,
        *,
        pack_ids: list[str],
        per_pack_limit: int,
        spaces: list[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """#437: top ``per_pack_limit`` FTS hits of EACH pack in ONE MATCH.

        Equivalent to calling ``keyword_search(query, pack_ids=[pid],
        limit=per_pack_limit, spaces=spaces)`` once per pack. The WHERE
        clause, the ``bm25`` rank and the order are the same. Equal ranks
        keep FTS scan order, as the tie tests check.
        The MATCH result set is ranked once and cut per pack with
        ``ROW_NUMBER() OVER (PARTITION BY pack)``, so a small pack's hits
        never compete with a large pack's hits. Packs without a hit are
        absent from the returned dict. Empty ``pack_ids``, ``per_pack_limit
        <= 0`` or an unavailable index return ``{}`` without querying.
        """
        if not self._available or not self._fts_ok:
            return {}
        if not pack_ids or per_pack_limit <= 0:
            return {}
        built = self._keyword_where(query, pack_ids, spaces)
        if built is None:
            return {}
        # Connection access comes last: on a fresh thread it opens a connection
        # and runs PRAGMAs, which the input guards above must not trigger.
        if not self._conn:
            return {}
        where_sql, params, pack_expr = built
        params.append(per_pack_limit)
        rows = self._conn.execute(
            # A MATCH query that projects bm25() and also uses it in a window
            # ORDER BY fails ("unable to use function bm25 in the requested
            # context"). The MATERIALIZED CTE computes rank in the MATCH query;
            # the window then runs over the CTE's rank column.
            "WITH m AS MATERIALIZED ("
            "SELECT f.source_id AS sid, s.text AS text, s.metadata AS meta, "
            f"bm25(doc_sources_fts) AS rank, {pack_expr} AS pid "
            "FROM doc_sources_fts f JOIN doc_sources s ON s.source_id = f.source_id "
            f"{where_sql}) "
            "SELECT sid, text, meta, rank, pid FROM ("
            "SELECT sid, text, meta, rank, pid, "
            "ROW_NUMBER() OVER (PARTITION BY pid ORDER BY rank) AS rn FROM m"
            ") WHERE rn <= ? ORDER BY pid, rn",
            params,
        ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            meta = json.loads(r["meta"]) if r["meta"] else {}
            out.setdefault(r["pid"], []).append({
                "source_id": r["sid"],
                "node_id": meta.get("node_id") or r["sid"],
                "text": r["text"],
                "metadata": meta,
                "score": -float(r["rank"] or 0.0),  # bm25: lower is better, so flip the sign
            })
        return out
